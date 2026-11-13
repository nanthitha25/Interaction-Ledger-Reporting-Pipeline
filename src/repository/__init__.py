"""Data access repository package for PostgreSQL entities."""
from src.repository.lead_repository import (
    LeadModel,
    LeadRepository,
    LeadRepositoryError,
)

__all__ = [
    "LeadModel",
    "LeadRepository",
    "LeadRepositoryError",
]
