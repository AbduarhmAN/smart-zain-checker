"""Server Module - Smart Zain Checker.
Contains HTTP REST API gateway (Port 5050) and Extension Bridge (Port 8766).
Decoupled network adapters: passes validated requests directly to the Orchestrator.
"""
from server.gateway import ServerGateway

__all__ = ["ServerGateway"]
