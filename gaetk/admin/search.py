#!/usr/bin/env python
# encoding: utf-8
"""
gaetk/admin/search.py

Created by Christian Klein on 2013-12-25.
Copyright (c) 2013 HUDORA GmbH. All rights reserved.
"""
import logging

from google.appengine.api import search

import gaetk.compat

from gaetk.admin import autodiscover
from gaetk.admin.util import get_app_name


INDEX_NAME = 'gaetk-admin'


# Füllt site._registry und kümmert sich um das Importieren der Modelklassen
autodiscover()


def fsearch(query_string, kind, limit=40, offset=0):
    """Suche nach Ersatzteilen und WebProducts"""

    expressions = [search.SortExpression(expression='aktiv',
                                         direction=search.SortExpression.DESCENDING),
                   search.SortExpression(expression='ausgegangen',
                                         direction=search.SortExpression.ASCENDING)]
    sort_options = search.SortOptions(expressions=expressions)

    options = search.QueryOptions(limit=limit,
                                  sort_options=sort_options,
                                  offset=offset)
    query_string = 'kind:%s %s' % (kind, query_string)
    hits, _cursor, total = perform_search(INDEX_NAME, query_string, options)
    return hits, total


def perform_search(indexname, query_string, options=None):
    """
    Führt eine Suche auf dem Suchindex aus

    Der Rückgabewert ist eine Liste der Suchergebnisse und ein Cursor,
    mit dem eine weitere Suche durchgeführt werden kann (per QueryOptions).
    Die Ergebnisse sind dicts, die den Dokumenten aus dem Index entsprechen,
    inklusive der Felder (document._fields).
    """

    results, cursor, total = [], None, 0
    index = search.Index(name=indexname)

    query = search.Query(query_string=query_string, options=options)
    try:
        # Die ScoredDocument-Instanzen haben ein Attribut _fields,
        # das eine Liste der Felder ist. Zum Anzeigen ist das leider eher
        # ungeeignet, daher werden hier dicts aus den Feldern erstellt,
        # bei denen zusätzlich 'doc_id' und 'rank' aus dem ScoredDocument übernommen wird.
        searchresult = index.search(query)
        cursor = searchresult.cursor
        for doc in searchresult:
            result = {'doc_id': doc.doc_id, 'rank': doc.rank}
            for field in doc.fields:
                result[field.name] = field.value
            results.append(result)
        total = searchresult.number_found
    except search.Error:
        logging.exception('Search for %r failed', query_string)

    return results, cursor, total


def add_to_index(key):
    """Füge Instanz dem Suchindex hinzu"""
    obj = gaetk.compat.xdb_get(key)
    if obj is None:
        return
    key = gaetk.compat.xdb_key(obj)
    key_name = gaetk.compat.xdb_id_or_name(key)
    skey = gaetk.compat.xdb_str_key(key)
    kind = gaetk.compat.xdb_kind(obj)

    # We have some very nasty problems with cyclic imports
    # site registry depends on options and options depends
    # on a lot of stuff which depends on the site registry
    # admin = gaetk.admin.sites.site._registry.get(type(obj))
    # if hasattr(admin, 'searchdoc'):
    #     data = admin.searchdoc(obj)

    if hasattr(obj, 'as_dict'):
        data = obj.as_dict()
    else:
        key_name = gaetk.compat.xdb_id_or_name(key)
        data = {'key_name': key_name}

    content = (value for value in data.itervalues() if isinstance(value, basestring))

    content = list(content)
    logging.debug(u'content: %s', content)

    fields = [search.TextField(name='key', value=skey),
              search.TextField(name='key_name', value=unicode(key_name)),
              search.TextField(name='str', value=unicode(obj)),
              search.TextField(name='kind', value=gaetk.compat.xdb_kind(obj)),
              search.TextField(name='app', value=get_app_name(obj)),
              search.TextField(name='content', value=' '.join(term for term in content if term)),
              ]

    document = search.Document(doc_id=skey, fields=fields)
    index = search.Index(name=INDEX_NAME)
    try:
        index.put(document)
    except search.Error:
        logging.info(u'Fehler beim Hinzufügen von %s %s zum Suchindex', kind, skey)


def remove_from_index(key):
    """Entferne Objekt aus Suchindex"""

    index = search.Index(name=INDEX_NAME)
    try:
        index.delete(gaetk.compat.xdb_str_key(key))
    except search.Error:
        logging.info(u'Fehler beim Löschen von %s %s aus Suchindex', key.kind(), key)
