"""Model access: applications ask for a purpose; the route table decides provider and model."""

from homelab.models.routes import Route, RouteTable, load_routes

__all__ = ["Route", "RouteTable", "load_routes"]
