class TmpfsError(Exception):
    """Base exception for tmpfs errors."""


class AttachError(TmpfsError):
    """Raised when allocating a RAM block device fails."""


class FormatError(TmpfsError):
    """Raised when formatting the RAM block device fails."""


class MountError(TmpfsError):
    """Raised when mounting the formatted device fails."""


class UnmountError(TmpfsError):
    """Raised when ejecting/unmounting a ramdisk fails."""


class ConfigError(TmpfsError):
    """Raised for invalid or conflicting configuration."""


class LaunchAgentError(TmpfsError):
    """Raised when installing/removing the login LaunchAgent fails."""
