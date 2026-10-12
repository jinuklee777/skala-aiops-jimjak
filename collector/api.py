"""data.go.kr 호출과 응답 해석.

응답 모양이 조금 달라도(items가 목록·단건·빈 값, JSON·XML) 받아들이고,
공공데이터포털 게이트웨이 오류는 코드로 가려서 다시 부를지 정한다.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from collector import __version__

OK_CODES = {"00", "03"}  # 03: 결과 없음
# 다시 불러도 같은 결과인 오류: 잘못된 요청(10·11·12), 권한·키(20·30·31·32·33), 일일 한도(22).
# 나머지(일시 장애, 초당 한도 23 등)는 재시도한다.
PERMANENT_CODES = {"10", "11", "12", "20", "22", "30", "31", "32", "33"}


class ApiError(Exception):
    def __init__(self, message, code=None, retryable=True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class Page:
    items: list
    total_count: int | None = None
    result_code: str | None = None


@dataclass
class Response:
    status: int | None  # 연결 자체가 안 됐으면 None
    headers: dict
    body: bytes | None
    elapsed_ms: int = 0
    error: str | None = None


@dataclass
class Outcome:
    page: Page | None
    error: str | None = None
    code: str | None = None
    retryable: bool = False


def build_url(base_url, operation, params, service_key) -> str:
    # 포털은 인코딩된 키와 안 된 키를 둘 다 준다. 어느 쪽을 넣어도 한 번만 인코딩되게 한다.
    key = urllib.parse.unquote(service_key) if "%" in service_key else service_key
    return f"{base_url}/{operation}?" + urllib.parse.urlencode((("serviceKey", key),) + tuple(params))


def redact(text, service_key):
    """오류 메시지에 키가 섞여 들어가도 기록에 남지 않게 지운다."""
    if not text or not service_key:
        return text
    plain = urllib.parse.unquote(service_key)
    for form in (service_key, plain, urllib.parse.quote_plus(plain), urllib.parse.quote(plain, safe="")):
        text = text.replace(form, "***")
    return text


def http_get(url, timeout) -> Response:
    request = urllib.request.Request(url, headers={"User-Agent": f"jimjak-collector/{__version__}"})
    started = time.monotonic()

    def elapsed():
        return round((time.monotonic() - started) * 1000)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return Response(response.status, _headers(response.headers), body, elapsed())
    except urllib.error.HTTPError as error:  # 4xx·5xx도 본문은 남긴다
        try:
            body = error.read()
        except (OSError, http.client.HTTPException):
            body = b""
        return Response(error.code, _headers(error.headers), body, elapsed())
    except (OSError, http.client.HTTPException) as error:  # 연결 실패, 시간 초과, 끊김
        return Response(None, {}, None, elapsed(), f"{type(error).__name__}: {error}")


def _headers(headers) -> dict:
    return {name: value for name, value in (headers or {}).items() if name.lower() != "set-cookie"}


def interpret(response: Response) -> Outcome:
    """응답을 받아들일지 정한다. 받아들이면 page가 있고, 아니면 오류와 재시도 여부가 있다."""
    if response.status is None:
        return Outcome(None, f"연결 실패: {response.error}", retryable=True)
    server_side = response.status >= 500 or response.status == 429
    try:
        page = parse_page(response.body or b"")
    except ApiError as error:
        if error.code is None and response.status != 200:
            return Outcome(None, f"HTTP {response.status}: {snippet(response.body)}", retryable=server_side)
        return Outcome(None, str(error), error.code, error.retryable)
    if response.status != 200:
        return Outcome(None, f"HTTP {response.status}", page.result_code, retryable=server_side)
    return Outcome(page, code=page.result_code)


def snippet(data, limit=120) -> str:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    data = " ".join(str(data or "").split())
    return data if len(data) <= limit else data[:limit] + "…"


def parse_page(body: bytes) -> Page:
    text = body.decode("utf-8", errors="replace").lstrip("﻿").strip()
    if not text:
        raise ApiError("빈 응답")
    if text.startswith("<"):
        return _parse_xml(text)
    try:
        data = json.loads(text)
    except ValueError:
        raise ApiError(f"해석할 수 없는 응답: {snippet(text)}") from None
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        raise ApiError(f"response가 없는 응답: {snippet(text)}")
    header = response.get("header") if isinstance(response.get("header"), dict) else {}
    code = _check(header.get("resultCode"), header.get("resultMsg"))
    body_part = response.get("body") if isinstance(response.get("body"), dict) else {}
    items = body_part.get("items")
    if isinstance(items, dict):
        items = items.get("item")
    if isinstance(items, dict):
        items = [items]
    if not items:
        items = []
    if not isinstance(items, list):
        raise ApiError(f"items 모양을 알 수 없는 응답: {snippet(text)}")
    return Page(items, _int(body_part.get("totalCount")), code)


def _parse_xml(text) -> Page:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ApiError(f"해석할 수 없는 응답: {snippet(text)}") from None
    if root.tag == "OpenAPI_ServiceResponse":  # 공공데이터포털 게이트웨이 오류
        code = (root.findtext(".//returnReasonCode") or "").strip() or None
        message = (root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or "").strip()
        raise ApiError(f"API 오류 {code}: {message}", code, retryable=code not in PERMANENT_CODES)
    if root.tag != "response":
        raise ApiError(f"알 수 없는 XML 응답: {snippet(text)}")
    code = _check(root.findtext("header/resultCode"), root.findtext("header/resultMsg"))
    items = [{child.tag: child.text or "" for child in item} for item in root.iter("item")]
    return Page(items, _int(root.findtext("body/totalCount")), code)


def _check(code, message):
    code = None if code is None else str(code).strip() or None
    if code is None or code in OK_CODES:
        return code
    raise ApiError(f"API 오류 {code}: {message}", code, retryable=code not in PERMANENT_CODES)


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
