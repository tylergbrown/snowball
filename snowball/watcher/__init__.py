"""The Watcher: official-macro research sidecar. Never places orders."""

__all__ = ["WatcherSidecar"]


def __getattr__(name: str):
    if name == "WatcherSidecar":
        from snowball.watcher.poller import WatcherSidecar

        return WatcherSidecar
    raise AttributeError(name)
