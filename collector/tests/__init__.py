import logging

# 수집기 로그(경고·알림)가 테스트 출력에 섞이지 않게 한다. 다른 로거는 건드리지 않는다.
_log = logging.getLogger("collector")
_log.addHandler(logging.NullHandler())
_log.propagate = False
