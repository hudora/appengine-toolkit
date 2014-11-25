"""Configuration for each AppEngine Instance"""

import config
config.imported = True

from gaetk.gaesessions import SessionMiddleware
COOKIE_KEY = '13f22fe8170a0a564fc2a26b8ffae123-5a17'


def webapp_add_wsgi_middleware(app):
    """Called with each WSGI handler initialisation """
    app = SessionMiddleware(app, cookie_key=COOKIE_KEY)
    # for https://github.com/kamens/gae_mini_profiler
    #app = gaetk.gae_mini_profiler.middleware.ProfilerWSGIMiddleware(app)
    return app

