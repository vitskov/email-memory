from .app import BrowserApp


def launch_browser(
    store, *, vector_store=None, provider_spec=None, provider_error: str | None = None,
) -> None:
    app = BrowserApp(
        store,
        vector_store=vector_store,
        provider_spec=provider_spec,
        provider_error=provider_error,
    )
    app.run()
