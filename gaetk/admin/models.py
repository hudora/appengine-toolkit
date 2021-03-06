#!/usr/bin/env python
# encoding: utf-8
"""
gaetk/admin/models.py - logging, audit und Archivierung.

Created by Dr. Maximillian Dornseif on 2011-09-30.
Copyright (c) 2011 HUDORA GmbH. All rights reserved.
"""

from google.appengine.ext import db


class DeletedObject(db.Model):
    """Hebt ein gelöschtes Model auf, um Undelete zu implementieren."""
    model_class = db.StringProperty(indexed=False)
    dblayer = db.StringProperty(default='db', indexed=False)
    old_key = db.StringProperty(indexed=False)
    data = db.BlobProperty(indexed=False)
    created_by = db.UserProperty(required=False, auto_current_user_add=True)
    updated_by = db.UserProperty(required=False, auto_current_user=True)
    created_at = db.DateTimeProperty(auto_now_add=True)
    updated_at = db.DateTimeProperty(auto_now=True)

    def undelete_url(self, abs_url=lambda x: x):
        """Returns the URL where an Object can be restored."""
        return abs_url('/admin/_undelete/%s' % self.key())
