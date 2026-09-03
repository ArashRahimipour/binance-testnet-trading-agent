"""Data-quality exceptions.

Every one of these is meant to be fail-closed: catching one anywhere in the
pipeline means "do not trade on this data", never "try to patch it up and
continue".
"""


class DataValidationError(Exception):
    """Base class for all data-quality failures."""


class DuplicateCandleError(DataValidationError):
    pass


class OutOfOrderCandleError(DataValidationError):
    pass


class GapDetectedError(DataValidationError):
    pass


class StaleDataError(DataValidationError):
    pass


class EmptyDataError(DataValidationError):
    pass
