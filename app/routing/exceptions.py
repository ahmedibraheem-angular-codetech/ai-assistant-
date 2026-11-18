class RoutingError(Exception):
    """The routing model did not return a usable decision."""


class InvalidRoutingResponseError(RoutingError):
    def __init__(self) -> None:
        super().__init__("The routing response could not be used.")
