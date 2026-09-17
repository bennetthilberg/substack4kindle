# Substack4Kindle

Substack4Kindle lets you proxy your Substack feeds to a simple web reader for Kindle browsers, so you can read Substack articles in a format the Kindle web browser can handle.

It syncs the signed-in user's All and Saved feeds through a bookmarklet run in a normal desktop browser. It stores sanitized article HTML in persistent storage and serves it as plain HTML. It does not generate EPUB files.

## Deploy

Run the container behind HTTPS and mount persistent storage at `/data`.

## Set up

1. Open `/claim` and set a reader password.
2. Open `/setup`.
3. Add the bookmarklet to a desktop browser.
4. Open Substack while signed in and run the bookmarklet.
5. Open the reader on the Kindle.

Substack blocks hosted headless sign-in, so the bookmarklet is required.
