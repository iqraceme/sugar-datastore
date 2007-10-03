""" 
xapianindex
~~~~~~~~~~~~~~~~~~~~
maintain indexes on content

""" 
from __future__ import with_statement

__author__ = 'Benjamin Saller <bcsaller@objectrealms.net>'
__docformat__ = 'restructuredtext'
__copyright__ = 'Copyright ObjectRealms, LLC, 2007'
__license__  = 'The GNU Public License V2+'



from Queue import Queue, Empty
import gc
import logging
import os
import re
import sys
import time
import thread
import threading
import warnings

import secore
import xapian as _xapian # we need to modify the QueryParser

from olpc.datastore import model 
from olpc.datastore.converter import converter
from olpc.datastore.utils import create_uid, parse_timestamp_or_float


# Setup Logger
logger = logging.getLogger('org.sugar.datastore.xapianindex')

# Indexer Operations
CREATE = 1
UPDATE = 2
DELETE = 3

ADD = 1
REMOVE = 2


class ContentMappingIter(object):
    """An iterator over a set of results from a search.

    """
    def __init__(self, results, backingstore, model):
        self._results = results
        self._backingstore = backingstore
        self._iter = iter(results)
        self._model = model

    def __iter__(self): return self
    
    def next(self):
        searchresult = self._iter.next()
        return model.Content(searchresult, self._backingstore, self._model)


class IndexManager(object):
    DEFAULT_DATABASE_NAME = 'index'
    
    def __init__(self, default_language='en'):
        # We will maintain two connections to the database
        # we trigger automatic flushes to the read_index
        # after any write operation        
        self.write_index = None
        self.read_index = None
        self.queue = Queue(0)
        self.indexer_running = False
        self.language = default_language

        self.backingstore = None
        
        self.fields = set()
        self._write_lock = threading.Lock()
    #
    # Initialization
    def connect(self, repo, **kwargs):
        if self.write_index is not None:
            warnings.warn('''Requested redundant connect to index''',
                          RuntimeWarning)

        self.repo = repo
        self.write_index = secore.IndexerConnection(repo)

        # configure the database according to the model
        datamodel = kwargs.get('model', model.defaultModel)
        datamodel.apply(self)

        # store a reference
        self.datamodel = datamodel
        
        self.read_index = secore.SearchConnection(repo)

        self.flush()        
        # by default we start the indexer now
        self.startIndexer()
        assert self.indexer.isAlive()

                
    def bind_to(self, backingstore):
        # signal from backingstore that its our parent
        self.backingstore = backingstore

    
    def stop(self, force=False):
        self.stopIndexer(force)
        self.write_index.close()
        self.read_index.close()
        # XXX: work around for xapian not having close() this will
        # change in the future in the meantime we delete the
        # references to the indexers and then force the gc() to run
        # which should inturn trigger the C++ destructor which forces
        # the database shut.
        self.write_index = None
        self.read_index = None
        gc.collect()
        
    # Index thread management
    def startIndexer(self):
        self.indexer_running = True
        self.indexer = threading.Thread(target=self.indexThread)
        self.indexer.setDaemon(True)
        self.indexer.start()
        
    def stopIndexer(self, force=False):
        if not self.indexer_running: return 
        if not force: self.queue.join()
        self.indexer_running = False
        # should terminate after the current task
        self.indexer.join()

    # flow control
    def flush(self):
        """Called after any database mutation"""
        with self._write_lock:
            self.write_index.flush()
            self.read_index.reopen()

    def enqueSequence(self, commands):
        """Takes a sequence of arugments to the normal enque function
        and executes them under a single lock/flush cycle
        """
        self.queue.put(commands)
        
    def enque(self, uid, vid, doc, operation, filestuff=None):
        # here we implement the sync/async policy
        # we want to take create/update operations and
        # set theproperties right away, the
        # conversion/fulltext indexing can
        # happen in the thread
        if operation in (CREATE, UPDATE):
            with self._write_lock:
                if operation is CREATE:
                    self.write_index.add(doc)
                    logger.info("created %s:%s" % (uid, vid))
                elif operation is UPDATE:
                    self.write_index.replace(doc)
                    logger.info("updated %s:%s" % (uid, vid))

            self.flush()
            # now change CREATE to UPDATE as we set the
            # properties already
            operation = UPDATE
            if not filestuff:
                # In this case we are done
                return
        elif operation is DELETE:
            # sync deletes
            with self._write_lock:
                self.write_index.delete(uid)
                logger.info("deleted content %s" % (uid,))
            self.flush()
            return
        
        self.queue.put((uid, vid, doc, operation, filestuff))

    def indexThread(self):
        # process the queue
        # XXX: there is currently no way to remove items from the queue
        # for example if a USB stick is added and quickly removed
        # the mount should however get a stop() call which would
        # request that the indexing finish
        # XXX: we can in many cases index, not from the tempfile but
        # from the item in the repo as that will become our immutable
        # copy. Detect those cases and use the internal filename
        # property or backingstore._translatePath to get at it
        versions = self.versions
        inplace = self.inplace
        q = self.queue
        while self.indexer_running:
            # include timeout here to ease shutdown of the thread
            # if this is a non-issue we can simply allow it to block
            try:
                # XXX: on shutdown there is a race where the queue is
                # joined while this get blocks, the exception seems
                # harmless though
                data = q.get(True, 0.025)
                # when we enque a sequence of commands they happen
                # under a single write lock pass through the loop and
                # the changes become visible at once.
                
                if not isinstance(data[0], (list, tuple)):
                    data = (data,)
            except Empty:
                continue

            try:
                with self._write_lock:
                    for item in data:
                        uid, vid, doc, operation, filestuff = item
                        if operation is DELETE:
                            self.write_index.delete(uid)
                            logger.info("deleted content %s" % (uid,))
                        elif operation is UPDATE:
                            # Here we handle the conversion of binary
                            # documents to plain text for indexing. This is
                            # done in the thread to keep things async and
                            # latency lower.
                            # we know that there is filestuff or it
                            # wouldn't have been queued
                            filename, mimetype = filestuff
                            if isinstance(filename, file):
                                filename = filename.name
                            if filename and not os.path.exists(filename):
                                # someone removed the file before
                                # indexing
                                # or the path is broken
                                logger.warning("Expected file for"
                                               " indexing at %s. Not"
                                               " Found" % filename)
                                
                            fp = converter(filename, mimetype=mimetype)
                            if fp:
                                # fixed size doesn't make sense, we
                                # shouldn't be adding fulltext unless
                                # it converted down to plain text in
                                # the first place
                                
                                while True:
                                    chunk = fp.read(2048)
                                    if not chunk: break
                                    doc.fields.append(secore.Field('fulltext', chunk))

                                self.write_index.replace(doc)
                                
                                if versions and not inplace:
                                    # we know the source file is ours
                                    # to remove 
                                    os.unlink(filename)
                                    
                                logger.info("update file content %s:%s" % (uid, vid))
                            else:
                                logger.debug("""Conversion process failed for document %s %s""" % (uid, filename))
                        else:
                            logger.warning("Unknown indexer operation ( %s: %s)" % (uid, operation))
                            
                    # tell the queue its complete 
                    self.queue.task_done()

                # we do flush on each record (or set for enque
                # sequences) now
                #self.flush()
            except:
                logger.exception("Error in indexer")
                

    def complete_indexing(self):
        """Intentionally block until the indexing is complete. Used
        primarily in testing.
        """
        self.queue.join()
        self.flush()
    
    #
    # Field management
    def addField(self, key, store=True, exact=False, sortable=False,
                 type='string', collapse=False,
                 **kwargs):
        language = kwargs.pop('language', self.language)
        
        xi = self.write_index.add_field_action
        
        if store: xi(key, secore.FieldActions.STORE_CONTENT)
        if exact: xi(key, secore.FieldActions.INDEX_EXACT)
        else:
            # weight -- int 1 or more
            # nopos  -- don't include positional information
            # noprefix -- boolean
            xi(key, secore.FieldActions.INDEX_FREETEXT, language=language, **kwargs)

        if sortable:
            xi(key, secore.FieldActions.SORTABLE, type=type)
        if collapse:
            xi(key, secore.FieldActions.COLLAPSE)

        # track this to find missing field configurations
        self.fields.add(key)

    #
    # Index Functions
    def _mapProperties(self, props):
        """data normalization function, maps dicts of key:kind->value
        to Property objects
        """
        d = {}
        add_anything = False
        for k,v in props.iteritems():
            p, added = self.datamodel.fromstring(k, v,
                                                 allowAddition=True)
            if added is True:
                self.fields.add(p.key)
                add_anything = True
            d[p.key] = p

        if add_anything:
            with self._write_lock:
                self.datamodel.apply(self)
            
        return d

    @property
    def versions(self):
        if self.backingstore:
            return "versions" in self.backingstore.capabilities
        return False

    @property
    def inplace(self):
        if self.backingstore:
            return "inplace" in self.backingstore.capabilities
        return False

    def _parse_tags(self, tags):
        # convert tags into (TAG, rev) pairs indicating if this tag
        # applies to this rev or all revs
        # all revs is ('tag', False)
        # the specific rev (unknown in this function is ('tag', True)
        t = tags.lower().split()
        r = []
        for tag in t:
            all = True
            mode = ADD
            if tag.startswith("-"):
                tag = tag[1:]
                mode = REMOVE
                
            if tag[-2:] == ":0":
                tag = tag[:-2]
                
            r.append((tag, all, mode))
                       
        return r
    
    def tag(self, uid, tags, rev=None):
        # this can't create items so we either resolve the uid (which
        # should be a given since we got to this layer) or fail
        results, count = self.get_by_uid_prop(uid, rev)
        if count == 0:
            raise KeyError('unable to apply tags to uid %s' % uid)

        # pull the whole version chain
        results = list(results)
        
        tags = self._parse_tags(tags)

        for tag, all, mode in tags:
            if all:
                used = results
            else:
                # select the revision indicated by rev
                # when None is provided this will be the tip
                pass

            if not tag and mode is REMOVE:
                # special case the '-' which removes all tags
                for c in used:
                    if 'tags' in c._doc.data:
                        del c._doc.data['tags']
            else:
                # not a global remove so we need to look at each
                # document, each tag and handle them case by case
                for c in used:
                    # we need to manipulate the field list of the existing
                    # docs and then replace them in the database
                    # to avoid adding new versions

                    # XXX: this should really be model driven and support
                    # any field that is of the tags type...
                    existing = set(c.get_property('tags', []))
                    if tag in existing and mode is REMOVE:
                        existing.remove(tag)
                    else:
                        existing.add(tag)
                        
                    # XXX: low level interface busting
                    # replace the current tags with the updated set
                    c._doc.data['tags'] = list(existing)
                    

        # Sync version, (enque with update for async)
        with self._write_lock:
            for c in results:
                self.write_index.replace_document(c)
        
        
    def index(self, props, filename=None):
        """Index the content of an object.
        Props must contain the following:
            key -> Property()
        """
        operation = UPDATE

        #
        # Version handling
        #
        # are we doing any special handling for versions?
        uid = props.pop('uid', None)

        
        if not uid:
            uid = create_uid()
            operation = CREATE
            
        
        # Property mapping via model
        doc = secore.UnprocessedDocument()
        add = doc.fields.append

        vid = None
        if self.versions:
            vid = props.get("vid")
            if not vid:
                logger.warn("Didn't supply needed versioning information"
                            " on a backingstore which performs versioning")
            # each versions id is unique when using a versioning store
            doc.id = create_uid()
        else:
            doc.id = uid

        if not vid: vid = '1'


        # on non-versioning stores this is redundant but on versioning
        # stores it reference to the objects whole timeline
        props['uid'] = uid
        props['vid'] = vid
              
        props = self._mapProperties(props)

        filestuff = None
        if filename:
            # enque async file processing
            # XXX: to make sure the file is kept around we could keep
            # and open fp?
            mimetype = props.get("mime_type")
            mimetype = mimetype and mimetype.value or 'text/plain'

            filename = os.path.abspath(filename)
            filestuff = (filename, mimetype)


        #
        # Property indexing
        for k, prop in props.iteritems():
            value = prop.for_xapian
            
            if k not in self.fields:
                warnings.warn("""Missing field configuration for %s""" % k,
                              RuntimeWarning)
                continue
            
            add(secore.Field(k, value))
            
        # queue the document for processing
        self.enque(uid, vid, doc, operation, filestuff)

        return doc.id

    def get(self, uid):
        doc = self.read_index.get_document(uid)
        if not doc: raise KeyError(uid)
        return model.Content(doc, self.backingstore, self.datamodel)

    def get_by_uid_prop(self, uid, rev=None):
        # versioning stores fetch objects by uid
        # when rev is passed only that particular rev is returne
        ri =  self.read_index
        q = ri.query_field('uid', uid)
        if rev is not None:
            if rev == "tip":
                rev = self.backingstore.tip(uid)
                
            q = ri.query_filter(q, ri.query_field('vid', str(rev)))
        results, count = self._search(q, 0, 1000, sortby="-vid")
        
        return results, count
        
        
    
    def delete(self, uid):
        # does this need queuing?
        # the higher level abstractions have to handle interaction
        # with versioning policy and so on
        self.enque(uid, None, None, DELETE)
        
    #
    # Search
    def search(self, query, start_index=0, end_index=4096, order_by=None):
        """search the xapian store.
        query is a string defining the serach in standard web search syntax.

        ie: it contains a set of search terms.  Each search term may be
        preceded by a "+" sign to indicate that the term is required, or a "-"
        to indicate that is is required to be absent.
        """
        ri = self.read_index

        if not query:
            q = self.read_index.query_all()
        elif isinstance(query, dict):
            queries = []
            q = query.pop('query', None)
            if q:
                queries.append(self.parse_query(q))
            if not query and not queries:
                # we emptied it 
                q = self.read_index.query_all()
            else:
                # each term becomes part of the query join
                for k, v in query.iteritems():
                    if isinstance(v, dict):
                        # it might be a range scan
                        # this needs to be factored out
                        # and/or we need client side lib that helps
                        # issue queries because there are type
                        # conversion issues here
                        start = v.pop('start', 0)
                        end = v.pop('end', sys.maxint)
                        start = parse_timestamp_or_float(start)
                        end = parse_timestamp_or_float(end)
                        queries.append(ri.query_range(k, start, end))
                    elif isinstance(v, list):
                        # construct a set of OR queries
                        ors = []
                        for item in v: ors.append(ri.query_field(k, item))
                        queries.append(ri.query_composite(ri.OP_OR, ors))
                    else:
                        queries.append(ri.query_field(k, v))
                        
                q = ri.query_composite(ri.OP_AND, queries)
        else:
            q = self.parse_query(query)

        if order_by and isinstance(order_by, list):
            # secore only handles a single item, not a multilayer sort
            order_by = order_by[0]
            
        return self._search(q, start_index, end_index, sortby=order_by)
    
    def _search(self, q, start_index, end_index, sortby=None):
        start_index = int(start_index)
        end_index = int(end_index)
        sortby = str(sortby)
        results = self.read_index.search(q, start_index, end_index, sortby=sortby)
        count = results.matches_estimated

        # map the result set to model.Content items
        return ContentMappingIter(results, self.backingstore, self.datamodel), count


    def get_uniquevaluesfor(self, property):
        # XXX: this is very sketchy code
        # try to get the searchconnection to support this directly
        # this should only apply to EXACT fields
        r = set()
        prefix = self.read_index._field_mappings.get_prefix(property)
        plen = len(prefix)
        termiter = self.read_index._index.allterms(prefix)
        for t in termiter:
            term = t.term
            if len(term) > plen:
                term = term[plen:]
                if term.startswith(':'): term = term[1:]
                r.add(term)

        # r holds the textual representation of the fields value set
        # if the type of field or property needs conversion to a
        # different python type this has to happen now
        descriptor = self.datamodel.fields.get(property)
        if descriptor:
            kind = descriptor[1]
            impl = model.propertyByKind(kind)
            r = set([impl.set(i) for i in r])
        
        return r
                                                         
    def parse_query(self, query):
        # accept standard web query like syntax
        # 'this' -- match this
        # 'this that' -- match this and that in document
        # '"this that"' match the exact pharse 'this that'
        # 'title:foo' match a document whose title contains 'foo'
        # 'title:"A tale of two datastores"' exact title match
        # '-this that' match that w/o this

        # limited support for wildcard searches
        qp = _xapian.QueryParser
        
        flags = (qp.FLAG_LOVEHATE)
        
        ri = self.read_index
        start = 0
        end = len(query)
        nextword = re.compile("(\S+)")
        endquote = re.compile('(")')
        queries = []
        while start < end:
            m = nextword.match(query, start)
            if not m: break
            orig = start
            field = None
            start = m.end() + 1
            word = m.group(1)
            if ':' in word:
                # see if its a field match
                fieldname, w = word.split(':', 1)
                if fieldname in self.fields:
                    field = fieldname
                    
                word = w

            if word.startswith('"'):
                qm = endquote.search(query, start)
                if qm:
                    #XXX: strip quotes or not here
                    #word = query[orig+1:qm.end(1)-1]
                    word = query[orig:qm.end(1)]
                    # this is a phase modify the flags
                    flags |= qp.FLAG_PHRASE
                    start = qm.end(1) + 1

            if field:
                queries.append(ri.query_field(field, word))
            else:
                if word.endswith("*"):
                    flags |= qp.FLAG_WILDCARD
                q = self._query_parse(word, flags)
                
                queries.append(q)
        q = ri.query_composite(ri.OP_AND, queries)
        return q



    def _query_parse(self, word, flags=0, op=None):
        # while newer secore do pass flags it doesn't allow control
        # over them at the API level. We override here to support
        # adding wildcard searching
        ri = self.read_index
        if op is None: op = ri.OP_AND
        qp = ri._prepare_queryparser(None, None, op)
        try:
            return qp.parse_query(word, flags)
        except _xapian.QueryParserError, e:
            # If we got a parse error, retry without boolean operators (since
            # these are the usual cause of the parse error).
            return qp.parse_query(string, 0)

        
