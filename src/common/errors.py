"""Application error hierarchy.

Every error that a handler is allowed to surface to a caller is an ``AppError``
carrying the HTTP status it maps to. Anything else escaping a service is a bug
and is reported as a 500 without leaking internals.
"""


class AppError(Exception):
    status_code = 500
    code = "InternalError"

    def __init__(self, message=None):
        self.message = message or self.__class__.__doc__ or "Unexpected error"
        super().__init__(self.message)


class ValidationError(AppError):
    """Request failed validation."""

    status_code = 400
    code = "ValidationError"


class NotFoundError(AppError):
    """Requested resource does not exist."""

    status_code = 404
    code = "NotFound"


class ConflictError(AppError):
    """Resource already exists."""

    status_code = 409
    code = "Conflict"


class ImageNotReadyError(AppError):
    """Image upload has not completed."""

    status_code = 409
    code = "ImageNotReady"


class UnsupportedMediaTypeError(AppError):
    """Content type is not an accepted image type."""

    status_code = 415
    code = "UnsupportedMediaType"


class StorageError(AppError):
    """A downstream AWS call failed."""

    status_code = 502
    code = "StorageError"
