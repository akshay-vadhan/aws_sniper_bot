from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env",), extra="ignore")
    data_mode: str = "live"
    database_url: str = "sqlite:///./radar.db"
    helius_api_key: str = ""
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    helius_enhanced_url: str = "https://api-mainnet.helius-rpc.com"
    alchemy_api_key: str = ""
    alchemy_solana_rpc_url: str = "https://solana-mainnet.g.alchemy.com/v2"
    wallet_poll_seconds: int = 30
    paper_bot_interval_seconds: int = 30
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    cluster_min_wallets: int = 3
    cluster_window_minutes: int = 10
    smart_money_min_score: int = 60
    cors_origins: str = "http://localhost:3000"


settings = Settings()
