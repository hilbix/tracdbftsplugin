# -*- coding: utf-8 -*-
#
# Copyright (C) 2021 Jun Omae
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

import os
import sys

from trac.admin.api import IAdminCommandProvider
from trac.attachment import Attachment
from trac.core import Component, TracError, implements
from trac.resource import ResourceNotFound
from trac.ticket.model import Milestone, Ticket
from trac.util.datefmt import from_utimestamp
from trac.util.text import console_print, exception_to_unicode, print_table
from trac.versioncontrol.api import (Changeset, NoSuchChangeset,
                                     RepositoryManager)
from trac.versioncontrol.cache import CachedRepository
from trac.wiki.model import WikiPage

from tracdbfts.api import TracDbftsSystem


class TracDbftsCommandProvider(Component):

    implements(IAdminCommandProvider)

    # IAdminCommandProvider methods

    def get_admin_commands(self):
        yield ('dbfts index', '[realms...]',
               'Build fulltext index for resources',
               None, self._do_index)
        yield ('dbfts search', 'terms...',
               'Search resources using fulltext index',
               None, self._do_search)

    # Internal methods

    def _do_index(self, *realms):
        if not realms:
            realms = ('wiki', 'ticket', 'milestone', 'changeset', 'attachment')
        out = sys.stderr
        isatty = hasattr(out, 'fileno') and os.isatty(out.fileno())

        def print_stat(n, realm, newline=True):
            if isatty:
                msg = 'Indexed %d objects from %s%s' % \
                      (n, realm, '' if newline else '\r')
                console_print(out, msg, newline=newline)

        mod = TracDbftsSystem(self.env)
        specs = {
            'wiki': (self._iter_wikis, mod.wiki_page_added),
            'ticket': (self._iter_tickets, mod.ticket_created),
            'milestone': (self._iter_milestones, mod.milestone_created),
            'changeset': (self._iter_changesets, mod.changeset_added),
            'attachment': (self._iter_attachments, mod.attachment_added),
        }
        with self.env.db_transaction as db:
            db("DELETE FROM dbfts WHERE realm IN ({0})"
               .format(','.join(['%s'] * len(realms))), realms)
            for realm in realms:
                iter_, add = specs[realm]
                n = 0
                print_stat(n, realm, newline=False)
                for n, item in enumerate(iter_(), 1):
                    if not isinstance(item, tuple):
                        item = [item]
                    add(*item)
                    print_stat(n, realm, newline=False)
                print_stat(n, realm)

    def _do_search(self, *terms):
        mod = TracDbftsSystem(self.env)
        max_ = 20
        header = ('realm', 'id', 'parent realm', 'parent id', 'score')
        results = []
        n = 0
        for n, result in enumerate(mod.search(terms), 1):
            if n <= max_:
                results.append(result)
        print_table(results, header)
        print('%d matches' % n)

    def _iter_wikis(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT DISTINCT name FROM wiki")
            for row in cursor:
                try:
                    page = WikiPage(self.env, row[0])
                except ResourceNotFound:
                    continue
                if page.exists:
                    yield page

    def _iter_tickets(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM ticket")
            for row in cursor:
                try:
                    ticket = Ticket(self.env, row[0])
                except ResourceNotFound:
                    continue
                yield ticket

    def _iter_milestones(self):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT name FROM milestone")
            for row in cursor:
                try:
                    milestone = Milestone(self.env, row[0])
                except ResourceNotFound:
                    continue
                yield milestone

    def _iter_changesets(self):
        manager = RepositoryManager(self.env)
        for reponame in manager.get_all_repositories():
            try:
                repos = manager.get_repository(reponame)
            except TracError as e:
                self.log.warning('Exception caught from RepositoryManager.'
                                 'get_repository(%r): %s',
                                 reponame, exception_to_unicode(e))
                continue
            iter_csets = self._iter_cached_csets \
                         if isinstance(repos, CachedRepository) else \
                         self._iter_normal_csets
            for cset in iter_csets(repos):
                yield repos, cset

    def _iter_cached_csets(self, repos):
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT rev, time, author, message FROM revision
                WHERE repos=%s
                """, (repos.id,))
            for rev, date, author, message in cursor:
                try:
                    repos.normalize_rev(rev)
                except NoSuchChangeset:
                    continue
                yield Changeset(repos, rev, message, author,
                                from_utimestamp(date))

    def _iter_normal_csets(self, repos):
        try:
            node = repos.get_node('')
        except Exception as e:
            self.log.warning('Exception caught from %s.get_node("") for '
                             'repository "%s": %s',
                             repos.__class__.__name__, repos.reponame,
                             exception_to_unicode(e))
            return
        for path, rev, change in node.get_history():
            try:
                cset = repos.get_changeset(rev)
            except Exception as e:
                self.log.warning('Exception caught from %s.get_changeset() '
                                 'for repository "%s": %s',
                                 repos.__class__.__name__, repos.reponame,
                                 exception_to_unicode(e))
            else:
                yield cset

    def _iter_attachments(self):
        query = "SELECT type, id, filename, description, size, time, author"
        if self.env.database_version < 42:
            query += ", ipnr"
        query += " FROM attachment ORDER BY type, id"
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute(query)
            for row in cursor:
                attachment = Attachment(self.env, row[0], row[1])
                attachment._from_database(*row[2:])
                yield attachment
