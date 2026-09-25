from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "production"
    plaid_products: str = "transactions"
    plaid_country_codes: str = "US"
    database_url: str = "sqlite:///./finance.db"
    # Optional: a Tiingo key makes Tiingo the primary price source, with
    # Yahoo Finance (no key) as the fallback.
    tiingo_api_key: str = ""

    @property
    def plaid_products_list(self) -> list[str]:
        return [p.strip() for p in self.plaid_products.split(",") if p.strip()]

    @property
    def plaid_country_codes_list(self) -> list[str]:
        return [c.strip() for c in self.plaid_country_codes.split(",") if c.strip()]


settings = Settings()
