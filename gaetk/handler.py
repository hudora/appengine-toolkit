#!/usr/bin/env python
# encoding: utf-8
"""
handler.py - default Request Handler

Created by Maximillian Dornseif on 2010-10-03.
Copyright (c) 2010 HUDORA. All rights reserved.
"""

# pylint can't handle db.Model.get()
# pylint: disable=E1103
# pylint can't handle google.appengine.api.memcache
# pylint: disable=E1101

import config
config.imported = True

try:
    LOGIN_ALLOWED_DOMAINS = config.LOGIN_ALLOWED_DOMAINS
except AttributeError:
    LOGIN_ALLOWED_DOMAINS = []


import os
import urllib
from functools import partial

from gaetk import webapp2
from gaetk.gaesessions import get_current_session
from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import db
from webob.exc import HTTPBadRequest as HTTP400_BadRequest
from webob.exc import HTTPConflict as HTTP409_Conflict
from webob.exc import HTTPForbidden as HTTP403_Forbidden
from webob.exc import HTTPFound as HTTP302_Found
from webob.exc import HTTPGone as HTTP410_Gone
from webob.exc import HTTPMovedPermanently as HTTP301_Moved
from webob.exc import HTTPNotAcceptable as HTTP406_NotAcceptable
from webob.exc import HTTPNotFound as HTTP404_NotFound
from webob.exc import HTTPNotImplemented as HTTP501_NotImplemented
from webob.exc import HTTPRequestEntityTooLarge as HTTP413_TooLarge
from webob.exc import HTTPSeeOther as HTTP303_SeeOther
from webob.exc import HTTPServiceUnavailable as HTTP503_ServiceUnavailable
from webob.exc import HTTPTemporaryRedirect as HTTP307_TemporaryRedirect
from webob.exc import HTTPUnauthorized as HTTP401_Unauthorized
from webob.exc import HTTPUnsupportedMediaType as HTTP415_UnsupportedMediaType

import base64
import google.appengine.ext.db
import google.appengine.runtime.apiproxy_errors
import hashlib
import jinja_filters
import logging
import time
import urlparse
import uuid


# to mark the exception as being used
config.dummy = [HTTP301_Moved, HTTP302_Found, HTTP303_SeeOther, HTTP307_TemporaryRedirect,
                HTTP400_BadRequest, HTTP403_Forbidden, HTTP404_NotFound, HTTP413_TooLarge,
                HTTP406_NotAcceptable, HTTP409_Conflict, HTTP410_Gone, HTTP415_UnsupportedMediaType,
                HTTP501_NotImplemented, HTTP503_ServiceUnavailable]


CREDENTIAL_CACHE_TIMEOUT = 300
_jinja_env_cache = {}


class Credential(db.Expando):
    """Represents an access token and somebody who is allowed to use it.

    Credentials MIGHT map to a google user object
    """
    tenant = db.StringProperty(required=False, default='_unknown')
    email = db.EmailProperty(required=False)
    user = db.UserProperty(required=False)
    uid = db.StringProperty(required=True)
    secret = db.StringProperty(required=True)
    text = db.StringProperty(required=False)
    admin = db.BooleanProperty(default=False)
    permissions = db.StringListProperty(default=['generic_permission'])
    created_at = db.DateTimeProperty(auto_now_add=True)
    updated_at = db.DateTimeProperty(auto_now=True)
    created_by = db.UserProperty(required=False, auto_current_user_add=True)
    updated_by = db.UserProperty(required=False, auto_current_user=True)

    @classmethod
    def create(cls, tenant='_unknown', user=None, uid=None, text='', email=None, admin=False):
        """Creates a credential Object generating a random secret and a random uid if needed."""
        # secret hopfully contains about 64 bits of entropy - more than most passwords
        data = u'%s%s%s%s%s%f%s' % (user, uuid.uuid1(), uid, text, email, time.time(),
                                    os.environ.get('CURRENT_VERSION_ID', '?'))
        digest = hashlib.md5(data.encode('utf-8')).digest()
        secret = str(base64.b32encode(digest).rstrip('='))[1:15]
        if not uid:
            handmade_key = db.Key.from_path('Credential', 1)
            uid = "u%s" % (db.allocate_ids(handmade_key, 1)[0])
        return cls.get_or_insert(key_name=uid, uid=uid, secret=secret, tenant=tenant,
                                 user=user, text=text, email=email, admin=admin)

    def __repr__(self):
        return "<gaetk.Credential %s>" % self.uid


def create_credential_from_federated_login(user, apps_domain):
    """Create a new credential object for a newly logged in OpenID user.

    This method provides a useful default implementation which should satisfy
    most needs one might have for new OpenID credentials. If however an application
    using the AppEngine toolkit does need to store more or different information
    it should overwrite this method by settings the configuration variable
    'LOGIN_OPENID_CREDENTIAL_CREATOR' in config.py to a custom method. The method
    must accept the same two arguments this method received. For an example you
    might want to look at HUDORA EDIhub, where an additional "receiver" gets written
    to the credentials database model.
    """
    credential = Credential.create(tenant=apps_domain, user=user, uid=user.email(),
        email=user.email(),
        text="Automatically created via OpenID Provider %s" % user.federated_provider(),
        # for accounts created via Google Apps domains we default to admin permissions
        admin=True)
    return credential


class BasicHandler(webapp2.RequestHandler):
    """Generischc Handler functionality.

    provides

    * `self.session` which si based on https://github.com/dound/gae-sessions.
    * `self.login_required(()` and `self.is_admin()` for Authentication
    * `self.authchecker()` to be overwritten to fully customize authentication
    """
    def __init__(self, *args, **kwargs):
        """Initialize RequestHandler"""
        self.credential = None
        self.session = get_current_session()
        super(BasicHandler, self).__init__(*args, **kwargs)

    def abs_url(self, url):
        """Converts an relative into an absolute URL."""
        return urlparse.urljoin(self.request.uri, url)

    def error(self, code):
        """Clears the response output stream and sets the given HTTP error code.

          code: the HTTP status error code (e.g., 501)
        """
        logging.info('Errorhandler')
        super(BasicHandler, self).error(code)
        if str(code) == '404':
            self.response.headers['Content-Type'] = 'text/plain'
            self.response.out.write('Daten nicht gefunden.')

    def paginate(self, query, defaultcount=10, datanodename='objects', calctotal=False, formatter=None):
        """Pagination a la http://mdornseif.github.com/2010/10/02/appengine-paginierung.html

        Returns something like
            {more_objects=True, prev_objects=True,
             prev_start=10, next_start=30,
             objects: [...], cursor='ABCDQWERY'}

        `formatter` is called for each object and can transfor it into something suitable.
        If no `formatter` is given and objects have a `as_dict()` method, this is used
        for formating.

        if `calctotal == True` then the total number of matching rows is given as an integer value. This
        is a ecpensive operation on the AppEngine and results might be capped at 1000.

        `datanodename` is the key in the returned dict, where the Objects resulting form the query resides.

        `defaultcount` is the default number of results returned. It can be overwritten with the
        HTTP-parameter `limit`.

        The `start` HTTP-parameter can skip records at the beginning of the result set.

        If the `cursor` HTTP-parameter is given we assume this is a cursor returned from an earlier query.
        See http://blog.notdot.net/2010/02/New-features-in-1-3-1-prerelease-Cursors and
        http://code.google.com/appengine/docs/python/datastore/queryclass.html#Query_cursor for
        further Information.
        """
        start = self.request.get_range('start', min_value=0, max_value=10000, default=0)
        limit = self.request.get_range('limit', min_value=1, max_value=1000, default=defaultcount)

        if self.request.get('cursor'):
            query.with_cursor(self.request.get('cursor'))
            objects = query.fetch(limit)
            start = self.request.get_range('cursor_start', min_value=0, max_value=10000, default=0)
            more_objects = (len(objects) == limit)
        else:
            # Attention: the order of these statements matter, because query.cursor() is used later.
            # If the order is reversed, the client gets a cursor to the query to test for more objects,
            # not a cursor to the actual objects
            more_objects = query.fetch(1, start + limit + 1) != []
            objects = query.fetch(limit, start)

        prev_objects = (start > 0) or self.request.get('cursor')
        prev_start = max(start - limit - 1, 0)
        next_start = max(start + len(objects), 0)
        clean_qs = dict([(k, self.request.get(k)) for k in self.request.arguments()
                         if k not in ['start', 'cursor', 'cursor_start']])
        ret = dict(more_objects=more_objects, prev_objects=prev_objects,
                   prev_start=prev_start, next_start=next_start)
        if more_objects:
            ret['cursor'] = query.cursor()
            ret['cursor_start'] = start + limit
            # query string to get to the next page
            qs = dict(cursor=ret['cursor'], cursor_start=ret['cursor_start'])
            qs.update(clean_qs)
            ret['next_qs'] = urllib.urlencode(qs)
        if prev_objects:
            # query string to get to the next previous page
            qs = dict(start=ret['prev_start'])
            qs.update(clean_qs)
            ret['prev_qs'] = urllib.urlencode(qs)
        if calctotal:
            # We count up to maximum of 10000. Counting in a somewhat expensive operation on AppEngine
            ret['total'] = query.count(10000)
        if formatter:
            ret[datanodename] = [formatter(x) for x in objects]
        else:
            ret[datanodename] = []
            for x in objects:
                ret[datanodename].append(x)
        return ret

    def default_template_vars(self, values):
        """Helper to provide additional values to HTML Templates. To be overwritten in subclasses. E.g.

            def default_template_vars(self, values):
                myval = dict(credential_empfaenger=self.credential_empfaenger,
                             navsection=None)
                myval.update(values)
                return myval
        """
        values.update({'is_admin': self.is_admin()})
        if self.is_admin():
            # for admin requests we import and activate the profiler
            import gae_mini_profiler.middleware
            values.update({'credential': self.credential,
                           'is_admin': self.is_admin(),
                           'profiler_request_id': gae_mini_profiler.middleware.request_id})
        return values

    def create_jinja2env(self, extensions=()):
        """Initialise and return a jinja2 Environment instance.

        Overwrite this method to setup specific behaviour.
        For example, to allow i18n:

            class myHandler(BasicHandler):
                def create_jinja2env(self):
                    import gettext
                    import jinja2
                    env = jinja2.Environment(extensions=['jinja2.ext.i18n'],
                                             loader=jinja2.FileSystemLoader(config.template_dirs))
                    env.install_gettext_translations(gettext.NullTranslations())
                    return env
        """
        import jinja2

        # Wir cachen das jinja2.Environment(). Dass ermögtlich es, dem internen Bytecode-Cache von
        # jinja2 zu greifen. Ich bin mir nicht sicher, ob das nicht mit dem kommenden
        # Mutlithreading-Support in GAE probleme machen wird - wir werden sehen.
        # Es spart jedenfalls bei komplexen Seiten, wie
        # http://hudora-de.appspot.com/shop/ersatzteil/95017 etwa 800 ms (!).
        # Der Schlüssel für den Cache sind die angeforderten Extensions.
        global _jinja_env_cache

        # Die Extensions müssen ein Tupel sein, eine Liste ist nicht hashable:
        # TypeError: unhashable type: 'list'
        key = tuple(extensions)
        if not key in _jinja_env_cache:
            env = jinja2.Environment(loader=jinja2.FileSystemLoader(config.template_dirs),
                                     extensions=extensions,
                                     auto_reload=True,  # do check if the source changed
                                     trim_blocks=True,  # first newline after a block is removed
                                     # This does not work (yet):
                                     # <type 'exceptions.RuntimeError'>: disallowed bytecode
                                     # bytecode_cache=jinja2.MemcachedBytecodeCache(memcache, timeout=600)
                                    )

            # Eigene Filter
            #env.filters['dateformat'] = filter_dateformat
            _jinja_env_cache[key] = env
        return _jinja_env_cache[key]

    def rendered(self, values, template_name):
        """Return the rendered content of a Jinja2 Template.

        Per default the template is provided with the `uri` and `credential` variables plus everything
        which is given in `values`."""
        import jinja2
        env = self.create_jinja2env()
        jinja_filters.register_custom_filters(env)
        try:
            template = env.get_template(template_name)
        except jinja2.TemplateNotFound:
            raise jinja2.TemplateNotFound(template_name)
        myval = dict(uri=self.request.url, credential=self.credential)
        myval.update(self.default_template_vars(values))
        self._expire_messages()
        myval.update(dict(_gaetk_messages=self.session.get('_gaetk_messages', [])))
        content = template.render(myval)
        return content

    def render(self, values, template_name):
        """Render a Jinja2 Template and wite it to the client."""
        self.response.out.write(self.rendered(values, template_name))

    def _expire_messages(self):
        new = []
        for message in self.session.get('_gaetk_messages', []):
            if message.get('expires', 0) > time.time():
                new.append(message)
        if len(new) != len(self.session.get('_gaetk_messages', [])):
            self.session['_gaetk_messages'] = new

    def multirender(self, fmt, data, mappers=None, contenttypes=None, filename='download',
                    defaultfmt='html', html_template='data', html_addon=None,
                    xml_root='data', xml_lists=None):
        """Multirender is meant to provide rendering for a variety of formats with minimal code.
        For the three major formats HTML, XML und JSON you can get away with virtually no code.
        Some real-world view method might look like this:

            # URL matches '/empfaenger/([A-Za-z0-9_-]+)/rechnungen\.?(json|xml|html)?',
            def get(self, kundennr, fmt):
                query = models.Rechnung.all().filter('kundennr = ', kundennr)
                values = self.paginate(query, 25, datanodename='rechnungen')
                self.multirender(fmt, values,
                                 filename='rechnungen-%s' % kundennr,
                                 html_template='rechnungen.html')

        `/empfaenger/12345/rechnungen` and `/empfaenger/12345/rechnungen.html` will result in
        `rechnungen.html` beeing rendered.
        `/empfaenger/12345/rechnungen.json` results in JSON being returned with a
        `Content-Disposition` header sending it to the file `rechnungen-12345.json`. Likewise for
        `/empfaenger/12345/rechnungen.xml`.
        If you add the Parameter `disposition=inline` no Content-Desposition header is generated.

        If you use fmt=json with a `callback` parameter, JSONP is generated. See
        http://en.wikipedia.org/wiki/JSONP#JSONP for details.

        IF you give a dict in `html_addon` this dict is additionaly passed the the HTML rendering function
        (but not to the rendering functions of other formats).

        You can give the `xml_root` and `xml_lists` parameters to provide `huTools.structured.dict2xml()`
        with defenitions on how to name elements. See the documentation of `roottag` and `listnames` in
        dict2xml documentation.

        For more sophisticated layout you can give customized mappers. Using functools.partial
        is very helpfiull for thiss. E.g.

            from functools import partial
            multirender(fmt, values,
                        mappers=dict(xml=partial(dict2xml, roottag='response',
                                                 listnames={'rechnungen': 'rechnung', 'odlines': 'odline'},
                                                  pretty=True),
                                     html=lambda x: '<body><head><title>%s</title></head></body>' % x))
        """

        # We lazy import huTools to keep gaetk usable without hutools
        import huTools.hujson
        import huTools.structured

        # If no format is given, we assume HTML (or whatever is given in defaultfmt)
        # We also provide a list of convinient default content types and encodings.
        fmt = fmt or defaultfmt
        mycontenttypes = dict(pdf='application/pdf',
                              xml="application/xml; encoding=utf-8",
                              json="application/json; encoding=utf-8",
                              html="text/html; encoding=utf-8",
                              csv="text/csv; encoding=utf-8",
                              invoic="application/edifact; encoding=iso-8859-1",
                              desadv="application/edifact; encoding=iso-8859-1")
        if contenttypes:
            mycontenttypes.update(contenttypes)

        # Default mappers are there for XML and JSON (provided by huTools) and HTML provided by Jinja2
        # we provide a default dict2xml renderer based on the xml_* parameters given
        mydict2xml = partial(huTools.structured.dict2xml, roottag=xml_root, listnames=xml_lists, pretty=True)
        # The HTML Render integrates additional data via html_addon
        htmldata = data.copy()
        if html_addon:
            htmldata.update(html_addon)
        htmlrender = lambda x: self.rendered(htmldata, html_template)
        mymappers = dict(xml=mydict2xml,
                         json=huTools.hujson.dumps,
                         html=htmlrender)
        if mappers:
            mymappers.update(mappers)

        # Check early if we have no corospondending configuration to provide more meaningful error messages.
        if fmt not in mycontenttypes:
            raise ValueError('No content-type for format "%r"' % contenttypes)
        if fmt not in mymappers:
            raise ValueError('No mapper for format "%r"' % fmt)

        # Disposition helps the browser to decide if something should be downloaded to disk or
        # if it should displayed in the browser window. It also can provide a filename.
        # per default we provide downloadable files
        if self.request.get('disposition') != 'inline':
            disposition = "attachment"
        else:
            disposition = "inline"

        if fmt not in ['html', 'json']:
            self.response.headers["Content-Disposition"] = \
                                "%s; filename=%s.%s" % (disposition, filename, fmt)
        # If we have gotten a `callback` parameter, we expect that this is a
        # [JSONP](http://en.wikipedia.org/wiki/JSONP#JSONP) can and therefore add the padding
        if self.request.get('callback', None) and fmt == 'json':
            response = "%s (%s)" % (self.request.get('callback', None), mymappers[fmt](data))
            self.response.headers['Content-Type'] = 'text/javascript'
        else:
            self.response.headers['Content-Type'] = mycontenttypes[fmt]
            response = mymappers[fmt](data)
        self.response.out.write(response)

    def is_admin(self):
        """Returns if the currently logged in user is admin"""
        # Google App Engine Administrators
        if users.is_current_user_admin():
            return True
        # Requests from localhost (on dev_appserver) are always admin
        if self.request.remote_addr == '127.0.0.1':
            return True
        # User with Admin permissions via Credential entities
        if not hasattr(self, 'credential'):
            return False
        elif self.credential is None:
            return False
        return self.credential.admin

    def login_required(self, deny_localhost=False):
        """Returns the currently logged in user and forces login.

        Access from 127.0.0.1 is allowed without authentication unless deny_localhost is `True`.
        """

        self.credential = None
        self.logintype = None
        if self.session.get('uid'):
            try:
                # try to read from memcache
                # we salt the cache object with the current app version, so data-migrations get easier
                self.credential = memcache.get("%s_gaetk_cred_%s"
                                               % (os.environ.get('CURRENT_VERSION_ID', '?'),
                                                  self.session['uid']))
            except AttributeError:
                # Unpickeling from memcache might fail becaus eof incompatible app versions etc.
                self.credential = None

            if self.credential is None:
                self.credential = Credential.get_by_key_name(self.session['uid'])
                # TODO: use protobufs
                memcache.add("gaetk_cred_%s" % self.session['uid'], self.credential, CREDENTIAL_CACHE_TIMEOUT)
                self.logintype = 'session'

        # we don't have an active session - check if we are logged in via OpenID at least
        user = users.get_current_user()
        if user:
            logging.info('Google user = %s', user)
            # yes, active OpenID session
            # user.federated_provider() == 'https://www.google.com/a/hudora.de/o8/ud?be=o8'
            if not user.federated_provider():
                # development server
                apps_domain = user.email().split('@')[-1].lower()
            else:
                apps_domain = user.federated_provider().split('/')[4].lower()
            username = user.email()
            self.credential = Credential.get_by_key_name(username)
            if not self.credential or not self.credential.uid == username:
                # So far we have no Credential entity for that user, create one
                if getattr(config, 'LOGIN_OPENID_CREDENTIAL_CREATOR', None):
                    self.credential = config.LOGIN_OPENID_CREDENTIAL_CREATOR(user, apps_domain)
                if not self.credential:
                    self.credential = create_credential_from_federated_login(user, apps_domain)
            self.session['uid'] = self.credential.uid
            self.logintype = 'OAuth'
            self.response.set_cookie('gaetkopid', apps_domain, max_age=7776000)

        if not self.credential:
            # still no session information - try HTTP - Auth
            uid, secret = None, None
            # see if we have HTTP-Basic Auth Data
            if self.request.headers.get('Authorization'):
                auth_type, encoded = self.request.headers.get('Authorization').split(None, 1)
                if auth_type.lower() == 'basic':
                    decoded = encoded.decode('base64')
                    # If the Client send us invalid credentials, let him know , else parse into
                    # username and password
                    if ':' not in decoded:
                        raise HTTP400_BadRequest("invalid credentials %r" % decoded)
                    uid, secret = decoded.split(':', 1)
                    # Pull credential out of memcache or datastore
                    credential = memcache.get("cred_%s" % uid)
                    if not credential:
                        credential = Credential.get_by_key_name(uid.strip() or ' *invalid* ')
                        memcache.add("cred_%s" % uid, credential, CREDENTIAL_CACHE_TIMEOUT)
                    if credential and credential.secret == secret.strip():
                        # Successful login
                        self.credential = credential
                        self.session['uid'] = credential.uid
                        self.session['email'] = credential.email
                        self.logintype = 'HTTP'
                        # Log successful login, but only once every 10h
                        data = memcache.get("login_%s_%s" % (uid, self.request.remote_addr))
                        if not data:
                            memcache.set("login_%s_%s" % (uid, self.request.remote_addr), True, 60 * 60 * 10)
                            logging.info("HTTP-Login from %s/%s", uid, self.request.remote_addr)
                    else:
                        logging.error("failed HTTP-Login from %s/%s %s", uid, self.request.remote_addr,
                                       self.request.headers.get('Authorization'))
                else:
                    logging.error("unknown HTTP-Login type %r %s %s", auth_type, self.request.remote_addr,
                                   self.request.headers.get('Authorization'))

        # HTTP Basic Auth failed
        if not self.credential:
            if (self.request.remote_addr == '127.0.0.1') and not deny_localhost:
                logging.info('for testing we allow unauthenticated access from localhost')
                # create credential
                self.credential = Credential.create(tenant='localhost.', uid='0x7f000001', admin=True,
                                                    text='Automatically created for testing')
            else:
                # Login not successful
                if 'text/html' in self.request.headers.get('Accept', ''):
                    # we assume the request came via a browser - redirect to the "nice" login page
                    self.response.set_status(302)
                    absolute_url = self.abs_url("/_ah/login_required?continue=%s"
                                                % urllib.quote(self.request.url))
                    self.response.headers['Location'] = str(absolute_url)
                    raise HTTP302_Found(location=str(absolute_url))
                else:
                    # We assume the access came via cURL et al, request Auth vie 401 Status code.
                    logging.info("requesting HTTP-Auth %s %s", self.request.remote_addr,
                                  self.request.headers.get('Authorization'))
                    raise HTTP401_Unauthorized(headers={'WWW-Authenticate': 'Basic realm="API Login"'})

        if self.credential.user and (not users.get_current_user()) and self.logintype != 'HTTP':
            # We have an active session and the credential is associated with an Federated/OpenID
            # Account, but the user is not logged in via OpenID on the gAE Infrastructure anymore.
            # If we are given tie desired domain via a coockit and this is a GET request
            # without parameters we try automatic login

            logging.critical("Session without gae! %s", self.request.cookies)
            if (self.request.cookies.get('gaetkopid', '') and self.request.method == 'GET'
                and not self.request.query_string):
                domain = self.request.cookies.get('gaetkopid', '')
                if domain in LOGIN_ALLOWED_DOMAINS:
                    openid_url = 'https://www.google.com/accounts/o8/site-xrds?hd=%s' % domain
                    logging.info('login: automatically OpenID login to %s', openid_url)
                    # Hand over Authentication Processing to Google/OpenID
                    # TODO: save get parameters in session
                    raise HTTP302_Found(location=users.create_login_url(self.request.path_url,
                                                                            None, openid_url))

        return self.credential

    def authchecker(self, method, *args, **kwargs):
        """Function to allow implementing authentication for all subclasses. To be overwritten."""
        pass

    def __call__(self, _method, *args, **kwargs):
        """Dispatches the requested method.

        :param _method:
            The method to be dispatched: the request method in lower case
            (e.g., 'get', 'post', 'head', 'put' etc).
        :param args:
            Positional arguments to be passed to the method, coming from the
            matched :class:`Route`.
        :param kwargs:
            Keyword arguments to be passed to the method, coming from the
            matched :class:`Route`.
        :returns:
            None.
        """
        method = getattr(self, _method, None)
        if method is None:
            # 405 Method Not Allowed.
            # The response MUST include an Allow header containing a
            # list of valid methods for the requested resource.
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html#sec10.4.6
            valid = ', '.join(webapp2.get_valid_methods(self))
            self.abort(405, headers=[('Allow', valid)])

        # bind session
        self.session = get_current_session()
        # init messages array
        self.session['_gaetk_messages'] = self.session.get('_gaetk_messages', [])
        # Give authentication Hooks opportunity to do their thing
        self.authchecker(method, *args, **kwargs)

        # Execute the method.
        method(*args, **kwargs)

    def handle_exception(self, exception, debug_mode):
        # This code is based on http://code.google.com/appengine/articles/handling_datastore_errors.html
        if (isinstance(exception, google.appengine.ext.db.Timeout)
            or isinstance(exception, google.appengine.ext.db.TransactionFailedError)):
            # TODO: Display "try again in a few seconds" message
            super(BasicHandler, self).handle_exception(exception, debug_mode)
        # This code is based on http://code.google.com/appengine/docs/python/howto/maintenance.html
        elif isinstance(exception, google.appengine.runtime.apiproxy_errors.CapabilityDisabledError):
            # Datastore is Read-Only
            # TODO: Display "try again in a hour" message
            super(BasicHandler, self).handle_exception(exception, debug_mode)
        else:
            if debug_mode:
                super(BasicHandler, self).handle_exception(exception, debug_mode)
            else:
                # TODO: Display a generic 500 error page.
                super(BasicHandler, self).handle_exception(exception, debug_mode)

    def add_message(self, typ, html, ttl=15):
        """Sets a user specified message to be displayed to the currently logged in user.

        `type` can be `error`, `success`, `info` or `warning`
        `html` is the text do be displayed
        `ttl` is the number of seconds after we should stop serving the message."""
        messages = self.session.get('_gaetk_messages', [])
        messages.append(dict(type=typ, html=html, expires=time.time() + ttl))
        # We can't use `.append()` because this doesn't result in automatic session saving.
        self.session['_gaetk_messages'] = messages


class JsonResponseHandler(BasicHandler):
    """Handler which is specialized for returning JSON.

    Excepts the method to return

    * dict(), e.g. `{'foo': bar}`
    * (dict(), int status), e.g. `({'foo': bar}, 200)`
    * (dict(), int status, int cachingtime), e.g. `({'foo': bar}, 200, 86400)`

    Dict is converted to JSON. `status` is used as HTTP status code. `cachingtime`
    is used to generate a `Cache-Control` header. If `cachingtime is None`, no header
    is generated. `cachingtime` defaults to two hours.
    """
    # Our default caching is 60s
    default_cachingtime = 60

    def __call__(self, _method, *args, **kwargs):
        """Dispatches the requested method. """

        # Lazily import hujson to allow using the other classes in this module to be used without
        # huTools beinin installed.
        import huTools.hujson

        # Find Method to be called.
        method = getattr(self, _method, None)
        if method is None:
            # No Mehtod is found.
            # Answer will be `405 Method Not Allowed`.
            # The response MUST include an Allow header containing a
            # list of valid methods for the requested resource.
            # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html#sec10.4.6
            # so get a lsit of valid Methods and send them back.
            valid = ', '.join(webapp2.get_valid_methods(self))
            # `self.abort()` will raise an Exception thus exiting this function
            self.abort(405, headers=[('Allow', valid)])

        # Give authentication hooks opportunity to do their thing
        self.authchecker(method, *args, **kwargs)

        # Execute the method.
        reply = method(*args, **kwargs)

        # find out which return convention was used - first set defaults ...
        content = reply
        statuscode = 200
        cachingtime = self.default_cachingtime
        # ... then check if we got a 2-tuple reply ...
        if isinstance(reply, tuple) and len(reply) == 2:
            content, statuscode = reply
        # ... or a 3-tuple reply.
        if isinstance(reply, tuple) and len(reply) == 3:
            content, statuscode, cachingtime = reply
        # Finally begin sending the response
        response = huTools.hujson.dumps(content)
        if cachingtime:
            self.response.headers['Cache-Control'] = 'max-age=%d, public' % cachingtime
        # If we have gotten a `callback` parameter, we expect that this is a
        # [JSONP](http://en.wikipedia.org/wiki/JSONP#JSONP) cann and therefore add the padding
        if self.request.get('callback', None):
            response = "%s (%s)" % (self.request.get('callback', None), response)
            self.response.headers['Content-Type'] = 'text/javascript'
        else:
            self.response.headers['Content-Type'] = 'application/json'
        # Set status code and write JSON to output stream
        self.response.set_status(statuscode)
        self.response.out.write(response)
        self.response.out.write('\n')
