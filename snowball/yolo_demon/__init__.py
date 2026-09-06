"""Yolo Demon: YouTube/X research sidecar. Never places orders, never writes HALT."""

__all__ = ["YoloDemonSidecar"]


def __getattr__(name: str):
    if name == "YoloDemonSidecar":
        from snowball.yolo_demon.poller import YoloDemonSidecar

        return YoloDemonSidecar
    raise AttributeError(name)
