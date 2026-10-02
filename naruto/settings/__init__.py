"""Typed settings stored in the database and edited in the web admin."""

from naruto.settings.registry import REGISTRY, SECTIONS, Setting, SettingError
from naruto.settings.service import SettingsService

__all__ = ["REGISTRY", "SECTIONS", "Setting", "SettingError", "SettingsService"]
