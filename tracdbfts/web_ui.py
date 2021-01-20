# -*- coding: utf-8 -*-
#
# Copyright (C) 2021 Jun Omae
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

from pkg_resources import parse_version
import re

from trac import __version__
from trac.core import Component, implements
from trac.resource import (Resource, get_resource_name, get_resource_shortname,
                           get_resource_url)
from trac.search.api import shorten_result
from trac.search.web_ui import SearchModule
from trac.ticket.api import TicketSystem
from trac.util.datefmt import datetime_now, from_utimestamp, utc
from trac.util.html import tag
from trac.util.text import shorten_line
from trac.util.translation import dgettext, tgettext_noop
from trac.versioncontrol.api import NoSuchChangeset, RepositoryManager
from trac.versioncontrol.cache import CachedRepository
from trac.web.api import IRequestFilter
from trac.web.chrome import Chrome, add_stylesheet

from tracdbfts.api import TracDbftsSystem


_parsed_version = parse_version(__version__)

if _parsed_version >= parse_version('1.4'):
    _use_jinja2 = True
elif _parsed_version >= parse_version('1.3'):
    _use_jinja2 = hasattr(Chrome, 'jenv')
else:
    _use_jinja2 = False


class TracDbftsSearchModule(Component):

    implements(IRequestFilter)

    # IRequestFilter methods

    _has_query_re = re.compile(r'(?:\A|&)q=[^&]')

    def pre_process_request(self, req, handler):
        if isinstance(handler, SearchModule) and \
                self._has_query_re.search(req.query_string):
            return self
        return handler

    def post_process_request(self, req, template, data, content_type):
        return template, data, content_type

    if _use_jinja2:
        def process_request(self, req):
            data = self._process_request(req)
            return 'search.html', data
    else:
        def process_request(self, req):
            data = self._process_request(req)
            return 'search.html', data, None

    # Internal methods

    def _process_request(self, req):
        search_mod = SearchModule(self.env)

        query = req.args.getfirst('q')
        terms = search_mod._parse_query(req, query) if query else None

        available_filters = []
        for source in search_mod.search_sources:
            available_filters.extend(source.get_search_filters(req) or [])
        available_filters.sort(key=lambda f: f[1].lower())
        filters = search_mod._get_selected_filters(req, available_filters)
        data = search_mod._prepare_data(req, query, available_filters, filters)

        if terms:
            results = self._do_search(req, terms, filters)
            if results:
                data.update(search_mod._prepare_results(req, filters, results))

        add_stylesheet(req, 'common/css/search.css')
        return data

    def _do_search(self, req, terms, filters):
        actions = {'wiki': 'WIKI_VIEW', 'ticket': 'TICKET_VIEW',
                   'milestone': 'MILESTONE_VIEW',
                   'changeset': 'CHANGESET_VIEW',
                   'attachment': 'ATTACHMENT_VIEW'}
        mod = TracDbftsSystem(self.env)
        results = []
        for result in mod.search(terms, filters):
            action = actions[result.realm]
            if result.parent_realm:
                res = Resource(result.parent_realm, result.parent_id) \
                     .child(result.realm, result.id)
            else:
                res = Resource(result.realm, result.id)
            if action in req.perm(res):
                results.append(result)

        entries = []
        for method in (self._prepare_results_wiki,
                       self._prepare_results_ticket,
                       self._prepare_results_milestone,
                       self._prepare_results_changeset,
                       self._prepare_results_attachment):
            entries.extend(method(req, terms, results))
        entries.sort(key=lambda entry: entry[0])
        for idx, entry in enumerate(entries):
            entries[idx] = entry[1:]
        return entries

    def _prepare_results_wiki(self, req, terms, results):
        wiki_realm = Resource('wiki')
        entries = dict((result.id, idx) for idx, result in enumerate(results)
                       if result.realm == 'wiki')
        if not entries:
            return

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT w1.name, w1.time, w1.author, w1.text
                FROM wiki w1, (SELECT name, max(version) AS ver
                               FROM wiki GROUP BY name) w2
                WHERE w1.version = w2.ver AND w1.name = w2.name
                AND w1.name IN ({0})
                """.format(','.join(['%s'] * len(entries))), list(entries))
            for name, ts, author, text in cursor:
                page = wiki_realm(id=name)
                yield (entries[name],
                       get_resource_url(self.env, page, req.href),
                       '%s: %s' % (name, shorten_line(text)),
                       from_utimestamp(ts), author,
                       shorten_result(text, terms))

    def _prepare_results_ticket(self, req, terms, results):
        ticket_realm = Resource('ticket')
        entries = dict((int(result.id), idx)
                       for idx, result in enumerate(results)
                       if result.realm == 'ticket')
        if not entries:
            return
        tktsys = TicketSystem(self.env)

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT summary, description, reporter, type, id, time, status,
                       resolution
                FROM ticket
                WHERE id IN ({0})
                """.format(','.join(['%s'] * len(entries))), list(entries))
            for summary, desc, author, type_, id_, ts, status, resolution \
                    in cursor:
                t = ticket_realm(id_)
                title = tgettext_noop(
                    dgettext_messages("%(title)s: %(message)s"),
                    title=tag.span(get_resource_shortname(self.env, t),
                                   class_=status),
                    message=tktsys.format_summary(summary, status, resolution,
                                                  type_))
                yield (entries[id_], req.href.ticket(id_), title,
                       from_utimestamp(ts), author,
                       shorten_result(desc, terms))

    def _prepare_results_milestone(self, req, terms, results):
        milestone_realm = Resource('milestone')
        entries = dict((result.id, idx) for idx, result in enumerate(results)
                       if result.realm == 'milestone')
        if not entries:
            return

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT name, due, completed, description FROM milestone
                WHERE name IN ({0})
                """.format(','.join(['%s'] * len(entries))), list(entries))
            for name, due, completed, description in cursor:
                milestone = milestone_realm(name)
                dt = (from_utimestamp(completed) if completed else
                      from_utimestamp(due) if due else datetime_now(utc))
                yield (entries[name],
                       get_resource_url(self.env, milestone, req.href),
                       get_resource_name(self.env, milestone), dt, '',
                       shorten_result(description, terms))

    def _prepare_results_changeset(self, req, terms, results):
        entries = dict(((result.parent_id, result.id), idx)
                       for idx, result in enumerate(results)
                       if result.realm == 'changeset')
        if not entries:
            return

        manager = RepositoryManager(self.env)
        repos_map = {}
        for reponame in set(parent_id for parent_id, id_ in entries):
            try:
                repos = manager.get_repository(reponame)
            except:
                continue
            else:
                repos_map[reponame] = repos
        if not repos_map:
            return

        def iter_cached(repos, revs):
            with self.env.db_query as db:
                cursor = db.cursor()
                args = [repos.id]
                args.extend(revs)
                cursor.execute("""\
                    SELECT rev, time, author, message FROM revision
                    WHERE repos=%s AND rev IN ({0})
                    """.format(','.join(['%s'] * len(revs))), args)
                for rev, ts, author, message in cursor:
                    try:
                        nrev = repos.normalize_rev(rev)
                        drev = repos.display_rev(nrev)
                    except NoSuchChangeset:
                        continue
                    yield nrev, drev, rev, from_utimestamp(ts), author, message

        def iter_direct(repos, revs):
            for rev in revs:
                try:
                    cset = repos.get_changeset(rev)
                    nrev = cset.rev
                    drev = repos.display_rev(nrev)
                except NoSuchChangeset:
                    continue
                yield nrev, drev, rev, cset.date, cset.author, cset.message

        href_cset = req.href.changeset
        for reponame, repos in repos_map.iteritems():
            revs = [id_ for parent_id, id_ in entries if parent_id == reponame]
            iter_ = iter_cached if isinstance(repos, CachedRepository) else \
                    iter_direct
            for nrev, drev, rev, date, author, message in iter_(repos, revs):
                yield (entries[(reponame, rev)],
                       href_cset(nrev, reponame or None),
                       '[%s]: %s' % (drev, shorten_line(message)),
                       date, author, shorten_result(message, terms))

    def _prepare_results_attachment(self, req, terms, results):
        entries = dict(((r.parent_realm, r.parent_id, r.id), idx)
                       for idx, r in enumerate(results)
                       if r.realm == 'attachment')
        if not entries:
            return

        with self.env.db_query as db:
            cursor = db.cursor()
            stmt = """\
                SELECT type, id, time, filename, description, author
                FROM attachment WHERE {0}
                """.format(' OR '.join('type=%s AND id=%s AND filename=%s'
                                       for idx in xrange(len(entries))))
            args = []
            for entry in entries:
                args.extend(entry)
            cursor.execute(stmt, args)
            for parent_realm, parent_id, ts, filename, desc, author in cursor:
                attachment = Resource(parent_realm, parent_id) \
                             .child('attachment', filename)
                yield (entries[(parent_realm, parent_id, filename)],
                       get_resource_url(self.env, attachment, req.href),
                       get_resource_shortname(self.env, attachment),
                       from_utimestamp(ts), author,
                       shorten_result(desc, terms))


def dgettext_messages(*args, **kwargs):
    return dgettext('messages', *args, **kwargs)
