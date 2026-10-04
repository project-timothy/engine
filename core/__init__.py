"""Business-agnostic back-office agent engine (core package).

Nothing under ``core/`` may name a business, a person, a vendor, an absolute
host path, or a specific tool account. That rule is enforced by the
bleed-through lint in CI. All tenant-specific values live under ``tenants/``.
"""
