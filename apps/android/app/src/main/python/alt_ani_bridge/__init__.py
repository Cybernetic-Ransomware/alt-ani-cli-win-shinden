"""JSON facade over the engine for a host application that cannot share Python objects (Android via Chaquopy)."""

from alt_ani_bridge.bridge import Bridge

__all__ = ["Bridge"]
