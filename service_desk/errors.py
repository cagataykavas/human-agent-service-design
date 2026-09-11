class ServiceDeskError(Exception):
    """Base class for expected service-desk failures."""


class NotFound(ServiceDeskError):
    pass


class Conflict(ServiceDeskError):
    pass


class Forbidden(ServiceDeskError):
    pass


class InvalidTransition(ServiceDeskError):
    pass


class ToolPolicyViolation(ServiceDeskError):
    pass
