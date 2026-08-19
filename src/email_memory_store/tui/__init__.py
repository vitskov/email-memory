from .app import BrowserApp


def launch_browser(store, *, vector_store=None) -> None:
    app = BrowserApp(store, vector_store=vector_store)
    app.run()
