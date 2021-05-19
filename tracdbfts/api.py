# -*- coding: utf-8 -*-
#
# Copyright (C) 2021 Jun Omae
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

from datetime import datetime
import base64
import collections
import hashlib
import re
import unicodedata
import sys

from trac.attachment import IAttachmentChangeListener
from trac.core import Component, TracError, implements
from trac.db.api import DatabaseManager
from trac.env import IEnvironmentSetupParticipant
from trac.ticket.api import IMilestoneChangeListener, ITicketChangeListener
from trac.util import lazy
from trac.util.datefmt import to_utimestamp
from trac.util.text import to_unicode
from trac.versioncontrol.api import IRepositoryChangeListener
from trac.versioncontrol.cache import CachedRepository
from trac.wiki.api import IWikiChangeListener
from trac.wiki.model import WikiPage


if sys.version_info[0] != 2:
    unicode = str
    basestring = str
    long = int
    _inttypes = (int,)
else:
    _inttypes = (int, long)


__all__ = ('SearchQuery', 'SearchResult', 'TracDbftsSystem')


class TracDbftsSystem(Component):

    implements(IEnvironmentSetupParticipant, IAttachmentChangeListener,
               IWikiChangeListener, ITicketChangeListener,
               IMilestoneChangeListener, IRepositoryChangeListener)

    # Public methods

    realms = ('wiki', 'ticket', 'milestone', 'changeset')

    def parse(self, query):
        return SearchQuery(query or '')

    def search(self, query, realms=None):
        if isinstance(query, unicode):
            query = self.parse(query)
            if query is None:
                raise ValueError('No terms in query parameter')
        if not isinstance(query, SearchQuery):
            raise TypeError('Must be a SearchQuery instance or an unicode '
                            'instance')
        return self._interface.search(query, realms or self.realms)

    # IEnvironmentSetupParticipant methods

    def environment_created(self):
        self._interface.create_schema()

    def environment_needs_upgrade(self, db=None):
        with self.env.db_query as db:
            if 'dbfts' not in db.get_table_names():
                return True

    def upgrade_environment(self, db=None):
        self._interface.create_schema()

    # IAttachmentChangeListener methods

    def attachment_added(self, attachment):
        hash_ = self._build_attachment_hash(attachment)
        content = self._build_attachment_content(attachment)
        self._insert_record(hash_, content, attachment.date, 'attachment',
                            attachment.filename, attachment.parent_realm,
                            attachment.parent_id)

    def attachment_deleted(self, attachment):
        hash_ = self._build_attachment_hash(attachment)
        self._delete_record(hash_)

    def attachment_reparented(self, attachment, old_parent_realm,
                              old_parent_id):
        with self.env.db_transaction:
            old_hash = _build_hash('attachment', attachment.filename,
                                   old_parent_realm, old_parent_id)
            self._delete_record(old_hash)
            self.attachment_added(attachment)

    # IWikiChangeListener methods

    def wiki_page_added(self, page):
        hash_ = self._build_wiki_hash(page)
        content = self._build_wiki_content(page)
        self._insert_record(hash_, content, page.time, page.realm, page.name)

    def wiki_page_changed(self, page, version, t, comment, author, ipnr=None):
        self._wiki_changed(page)

    def wiki_page_deleted(self, page):
        hash_ = self._build_wiki_hash(page)
        self._delete_record(hash_)

    def wiki_page_version_deleted(self, page):
        page = WikiPage(self.env, page.name)  # fetch latest version
        self._wiki_changed(page)

    def wiki_page_renamed(self, page, old_name):
        with self.env.db_transaction:
            old_page = WikiPage(self.env, old_name)
            old_key = _build_hash(page.realm, old_name)
            if old_page.exists:
                content = self._build_wiki_content(old_page)
                self._update_record(old_key, content, old_page.time)
            else:
                self._delete_record(old_key)
            self.wiki_page_added(page)

    # ITicketChangeListener methods

    def ticket_created(self, ticket):
        hash_ = self._build_ticket_hash(ticket)
        content = self._build_ticket_content(ticket)
        self._insert_record(hash_, content, ticket['time'], 'ticket',
                            ticket.id)

    def ticket_changed(self, ticket, comment, author, old_values):
        self._ticket_changed(ticket)

    def ticket_deleted(self, ticket):
        hash_ = self._build_ticket_hash(ticket)
        self._delete_record(hash_)

    def ticket_comment_modified(self, ticket, cdate, author, comment,
                                old_comment):
        self._ticket_changed(ticket)

    def ticket_change_deleted(self, ticket, cdate, changes):
        self._ticket_changed(ticket)

    # IMilestoneChangeListener methods

    def milestone_created(self, milestone):
        hash_ = self._build_milestone_hash(milestone)
        content = self._build_milestone_content(milestone)
        self._insert_record(hash_, content,
                            milestone.completed or milestone.due,
                            'milestone', milestone.name)

    def milestone_changed(self, milestone, old_values):
        hash_ = self._build_milestone_hash(milestone)
        content = self._build_milestone_content(milestone)
        self._update_record(hash_, content,
                            milestone.completed or milestone.due)

    def milestone_deleted(self, milestone):
        hash_ = self._build_milestone_hash(milestone)
        self._delete_record(hash_)

    # IRepositoryChangeListener methods

    def changeset_added(self, repos, changeset):
        rev = _db_rev(changeset)
        hash_ = self._build_changeset_hash(repos, changeset)
        content = self._build_changeset_content(changeset)
        self._insert_record(hash_, content, changeset.date, 'changeset', rev,
                            'repository', repos.reponame)

    def changeset_modified(self, repos, changeset, old_changeset):
        hash_ = self._build_changeset_hash(repos, changeset)
        content = self._build_changeset_content(changeset)
        self._update_record(hash_, content, changeset.date)

    # Internal methods

    @lazy
    def _database_scheme(self):
        dbm = DatabaseManager(self.env)
        return dbm.connection_uri.split(':', 1)[0]

    @lazy
    def _interface(self):
        type_ = self._database_scheme
        if type_ == 'mysql':
            ctor = MySQLDbftsInterface
        elif type_ == 'postgres':
            ctor = PostgreSQLDbftsInterface
        elif type_ == 'sqlite':
            ctor = SQLiteDbftsInterface
        else:
            raise ValueError('%r is not supported' % type_)
        return ctor(self.env)

    def _insert_record(self, hash_, content, time, realm, id_,
                       parent_realm=None, parent_id=None):
        stmt = """\
            INSERT INTO dbfts (hash,time,realm,id,parent_realm,parent_id,
                               content)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)"""
        if time is not None and isinstance(time, datetime):
            time = to_utimestamp(time)
        if id_ is not None and isinstance(id_, basestring):
            id_ = to_unicode(id_)
        if parent_id is not None and isinstance(parent_id, basestring):
            parent_id = to_unicode(parent_id)
        self.env.db_transaction(stmt, (hash_, time, realm, id_, parent_realm,
                                       parent_id, content))

    def _update_record(self, hash_, content, time):
        if time is not None and isinstance(time, datetime):
            time = to_utimestamp(time)
        self.env.db_transaction("UPDATE dbfts SET content=%s, time=%s "
                                "WHERE hash=%s", (content, time, hash_))

    def _delete_record(self, hash_):
        self.env.db_transaction("DELETE FROM dbfts WHERE hash=%s", (hash_,))

    def _wiki_changed(self, page):
        hash_ = self._build_wiki_hash(page)
        content = self._build_wiki_content(page)
        self._update_record(hash_, content, page.time)

    def _ticket_changed(self, ticket):
        hash_ = self._build_ticket_hash(ticket)
        content = self._build_ticket_content(ticket)
        self._update_record(hash_, content, ticket['changetime'])

    def _build_attachment_hash(self, attachment):
        return _build_hash('attachment', attachment.filename,
                           attachment.parent_realm, attachment.parent_id)

    def _build_attachment_content(self, attachment):
        return self._build_content(attachment.filename, attachment.description,
                                   attachment.author)

    def _build_wiki_hash(self, page):
        return _build_hash(page.realm, page.name)

    def _build_wiki_content(self, page):
        return self._build_content(page.name, page.author, page.text)

    def _build_ticket_hash(self, ticket):
        return _build_hash('ticket', ticket.id)

    def _build_ticket_content(self, ticket):
        values = [ticket['summary'], ticket['keywords'], ticket['description'],
                  ticket['reporter'], ticket['cc'], ticket.id]
        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("SELECT newvalue FROM ticket_change "
                           "WHERE ticket=%s", (ticket.id,))
            values.extend(row[0] for row in cursor)
            cursor.execute("SELECT value FROM ticket_custom WHERE ticket=%s",
                           (ticket.id,))
        return self._build_content(*values)

    def _build_milestone_hash(self, milestone):
        return _build_hash('milestone', milestone.name)

    def _build_milestone_content(self, milestone):
        return self._build_content(milestone.name, milestone.description)

    def _build_changeset_hash(self, repos, cset):
        return _build_hash('changeset', _db_rev(cset), 'repository',
                           repos.reponame)

    def _build_changeset_content(self, cset):
        return self._build_content(cset.rev, cset.message, cset.author)

    def _build_content(self, *values):
        dbnorm = self._interface.normalize
        return u'\n\n'.join(dbnorm(_normalize(value)) for value in values)


TOKEN_OR = list
class TOKEN_NOT(unicode): pass


class SearchQuery(object):

    __slots__ = ('query', 'items', 'terms')

    def __init__(self, query):
        items = self._parse(query)
        terms = {}
        for item in items:
            if isinstance(item, TOKEN_OR):
                for v in item:
                    if not isinstance(v, TOKEN_NOT):
                        terms.setdefault(v, len(terms))
            elif not isinstance(item, TOKEN_NOT):
                terms.setdefault(item, len(terms))
        terms = tuple(sorted(terms, key=lambda term: terms[term]))
        self.query = query
        self.items = items
        self.terms = terms

    _parse_re = re.compile(r"""
        (?: \S+
          | "[^"]*(?:"|\Z)
          | '[^']*(?:'|\Z)
        )
    """, re.UNICODE | re.VERBOSE)

    _normalize_re = re.compile(r'[\x00-\x1f\x7f]+')

    def _parse(self, query):

        def generate(query):
            query = self._normalize_re.sub(' ', query)
            for match in self._parse_re.finditer(query):
                yield match.group(0)

        def next_token(iterator):
            while True:
                try:
                    token = next(iterator)
                except StopIteration:
                    return None
                if token == 'OR':
                    return TOKEN_OR
                if token.startswith('-'):
                    negative = True
                    token = token[1:]
                else:
                    negative = False
                if token.startswith('"'):
                    token = token.strip('"')
                elif token.endswith("'"):
                    token = token.strip("'")
                if token:
                    return TOKEN_NOT(token) if negative else token

        items = []
        iterator = generate(query)
        while True:
            token = next_token(iterator)
            if token is None:
                break
            if token is TOKEN_OR:
                if not items:
                    continue
                while token is TOKEN_OR:
                    token = next_token(iterator)
                if token is None:
                    break
                last = items[-1]
                if not isinstance(last, TOKEN_OR):
                    items[-1] = last = [last]
                last.append(token)
                continue
            items.append(token)

        return [item[0] if isinstance(item, TOKEN_OR) and len(item) == 1 else
                item for item in items]


SearchResult = collections.namedtuple(
    'SearchResult', 'time realm id parent_realm parent_id score')


class DbftsInterface(object):

    def __init__(self, env):
        self.env = env
        self.config = env.config
        self.log = env.log

    def create_schema(self):
        raise NotImplementedError

    def search(self, query, realms):
        raise NotImplementedError

    def normalize(self, values):
        raise NotImplementedError


class MySQLDbftsInterface(DbftsInterface):

    def create_schema(self):
        with self.env.db_transaction as db:
            charset = db.charset
            cursor = db.cursor()
            cursor.execute("""\
                CREATE TABLE dbfts (
                    hash            VARBINARY(27) NOT NULL,
                    time            BIGINT,
                    realm           MEDIUMTEXT,
                    id              MEDIUMTEXT,
                    parent_realm    MEDIUMTEXT,
                    parent_id       MEDIUMTEXT,
                    content         MEDIUMTEXT COLLATE {0}_general_ci,
                    PRIMARY KEY (hash),
                    FULLTEXT KEY key_dbfts_content (content) WITH PARSER ngram)
                """.format(charset))

    def search(self, query, realms):
        query = self._build_query(query)
        args = [query, query]
        args.extend(realms)
        args.extend(realms)

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT time, realm, id, parent_realm, parent_id,
                       MATCH (content) AGAINST (%s IN BOOLEAN MODE) AS score
                FROM dbfts
                WHERE MATCH (content) AGAINST (%s IN BOOLEAN MODE)
                AND (realm IN ({0}) OR parent_realm IN ({0}))
                ORDER BY score DESC
                """.format(','.join(['%s'] * len(realms))), args)
            for row in cursor:
                yield SearchResult(*row)

    def normalize(self, value):
        return value

    def _build_query(self, query):

        def to_expr(item, or_=False):
            if isinstance(item, TOKEN_OR):
                expr = ' '.join(to_expr(v, or_=True) for v in item)
                return '+( %s )' % expr
            else:
                negative = isinstance(item, TOKEN_NOT)
                item = item.replace('"', ' ').strip()
                item = ('%s*' if item[:1].isalnum() and ' ' not in item else
                        '"%s"*') % item
                if or_:
                    return item
                else:
                    return ('-' if negative else '+') + item

        return ' '.join(to_expr(item) for item in query.items)


class PostgreSQLDbftsInterface(DbftsInterface):

    def create_schema(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute("""\
                CREATE TABLE dbfts (
                    hash            BYTEA,
                    time            BIGINT,
                    realm           TEXT,
                    id              TEXT,
                    parent_realm    TEXT,
                    parent_id       TEXT,
                    content         TEXT,
                    PRIMARY KEY (hash))
                """)
            cursor.execute("""\
                CREATE INDEX key_dbfts_content ON dbfts
                    USING gin (content public.gin_bigm_ops)
                """)

    def search(self, query, realms):
        with self.env.db_query as db:
            condition, args = self._build_query(query, db.like_escape)
            args.extend(realms)
            args.extend(realms)
            cursor = db.cursor()
            cursor.execute("""\
                SELECT time, realm, id, parent_realm, parent_id, NULL AS score
                FROM dbfts
                WHERE {0} AND (realm IN ({1}) OR parent_realm IN ({1}))
                ORDER BY time DESC
                """.format(condition, ','.join(['%s'] * len(realms))),
                args)
            for row in cursor:
                yield SearchResult(*row)

    def normalize(self, value):
        return unicodedata.normalize('NFKC', value).lower()

    def _build_query(self, query, like_escape):
        exprs = []
        args = []
        for item in query.items:
            if isinstance(item, TOKEN_OR):
                exprs.append("(%s)" % ' OR '.join(["content LIKE %s ESCAPE "
                                                   "'/'"] * len(item)))
                args.extend(self.normalize(v) for v in item)
            else:
                if isinstance(item, TOKEN_NOT):
                    exprs.append("content NOT LIKE %s ESCAPE '/'")
                else:
                    exprs.append("content LIKE %s ESCAPE '/'")
                args.append(self.normalize(item))

        args = ['%' + like_escape(arg) + '%' for arg in args]
        return ' AND '.join(exprs), args


class SQLiteDbftsInterface(DbftsInterface):

    def create_schema(self):
        with self.env.db_transaction as db:
            cursor = db.cursor()
            cursor.execute("PRAGMA module_list")
            if not any(row[0] == 'fts5' for row in cursor):
                raise TracError("fts5 module unavailable")
            cursor.execute("""\
                CREATE TABLE dbfts (
                    pkey            INTEGER PRIMARY KEY,
                    hash            BLOB UNIQUE NOT NULL,
                    time            INTEGER,
                    realm           TEXT,
                    id              TEXT,
                    parent_realm    TEXT,
                    parent_id       TEXT,
                    content         TEXT)
                """)
            cursor.execute("DROP TABLE IF EXISTS dbfts_idx")
            cursor.execute("DROP TRIGGER IF EXISTS dbfts_insert")
            cursor.execute("DROP TRIGGER IF EXISTS dbfts_insert")
            cursor.execute("DROP TRIGGER IF EXISTS dbfts_delete")
            cursor.execute("""\
                CREATE VIRTUAL TABLE dbfts_idx
                    USING fts5(content, tokenize=trigram,
                               content=dbfts, content_rowid=pkey)
                """)
            cursor.execute("""\
                CREATE TRIGGER dbfts_insert AFTER INSERT ON dbfts BEGIN
                    INSERT INTO dbfts_idx (rowid, content)
                                VALUES (new.pkey, new.content);
                END
                """)
            cursor.execute("""\
                CREATE TRIGGER dbfts_delete AFTER DELETE ON dbfts BEGIN
                    INSERT INTO dbfts_idx(dbfts_idx, rowid)
                                VALUES ('delete', old.pkey);
                END
                """)
            cursor.execute("""\
                CREATE TRIGGER dbfts_update AFTER UPDATE ON dbfts BEGIN
                    INSERT INTO dbfts_idx(dbfts_idx, rowid)
                                VALUES ('delete', old.pkey);
                    INSERT INTO dbfts_idx (rowid, content)
                                VALUES (new.pkey, new.content);
                END
                """)

    def search(self, query, realms):
        query = self._build_query(query)
        args = [query]
        args.extend(realms)
        args.extend(realms)

        with self.env.db_query as db:
            cursor = db.cursor()
            cursor.execute("""\
                SELECT d.time, d.realm, d.id, d.parent_realm, d.parent_id, rank
                FROM dbfts AS d, dbfts_idx AS i
                WHERE i.content MATCH %s AND d.pkey=i.rowid
                AND (d.realm IN ({0}) OR d.parent_realm IN ({0}))
                ORDER BY rank
                """.format(','.join(['%s'] * len(realms))), args)
            for row in cursor:
                yield SearchResult(*row)

    def normalize(self, value):
        return unicodedata.normalize('NFKC', value)

    def _build_query(self, query):

        def to_expr(item, or_=False):
            if isinstance(item, TOKEN_OR):
                return '(%s)' % ' OR '.join(to_expr(v, or_=True) for v in item)
            else:
                negative = isinstance(item, TOKEN_NOT)
                item = self.normalize(item).replace('"', ' ').strip()
                if not item.isalnum() or item in ('AND', 'NOT', 'OR'):
                    item = '"%s"' % item
                if or_:
                    return item
                elif negative:
                    return 'NOT %s' % item
                else:
                    return item

        items = sorted(query.items,
                       key=lambda item: isinstance(item, TOKEN_NOT))
        exprs = [to_expr(item) for item in items]
        return ' '.join(expr if idx == 0 or expr.startswith('NOT ') else
                        'AND ' + expr for idx, expr in enumerate(exprs))


def _build_hash(*values):
    def to_b(value):
        if isinstance(value, _inttypes):
            return b'%d' % value
        if isinstance(value, bytes):
            return value
        if isinstance(value, unicode):
            return value.encode('utf-8')
        raise ValueError('Unrecognized value %r' % type(value))
    d = hashlib.sha1()
    d.update(b'\0'.join(to_b(value) for value in values))
    return base64.b64encode(d.digest()).rstrip(b'=')


def _db_rev(cset):
    rev = cset.rev
    if isinstance(rev, basestring):
        return to_unicode(rev)
    repos = cset.repos
    if isinstance(repos, CachedRepository):
        return repos.db_rev(rev)
    if isinstance(rev, int):  # e.g. direct-svnfs
        return '%010d' % cset.rev
    return rev


_spaces_re = re.compile(r'\s+')

def _normalize(value):
    if value is None:
        return u''
    if isinstance(value, (int, long)):
        return unicode(value)
    if isinstance(value, bytes):
        try:
            value = unicode(value, 'utf-8')
        except:
            value = unicode(value, 'latin1')
    if isinstance(value, unicode):
        return _spaces_re.sub(' ', value)
    raise ValueError('Unrecognized value %r' % type(value))
