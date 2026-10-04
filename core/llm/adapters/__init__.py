"""Provider adapters for the model gateway. Each module defines one class
satisfying ``core.llm.Adapter``; nothing here is imported eagerly, so a
provider's client library loads only when that adapter is used."""
