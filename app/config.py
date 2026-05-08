from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )
    gemini_api_key: str = ""
    slack_webhook_url: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    app_timezone: str = "Asia/Kolkata"

    # The Google service account creds below are used for both Sheets and Calendar.
    # `google_calendar_id` defaults to "primary" — works when the rep has shared their
    # primary calendar with the service-account email. For domain-wide delegation,
    # set this to the rep's email (e.g. "rep@joveo.com") and configure delegation in Workspace.
    google_calendar_id: str = "primary"

    slack_default_channel: str = "#supply-partnership-product"

    google_credentials_json: str = ""
    google_sheet_id: str = ""

    # OAuth client used when a rep signs in with Google so the app can read that
    # rep's own primary calendar instead of a single service-account calendar.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""

    # Used to sign the lightweight login cookie. Generate a long random value
    # for production and keep it out of git.
    app_secret_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


settings = Settings()
