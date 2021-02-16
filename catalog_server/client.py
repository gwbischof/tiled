import collections.abc
from dataclasses import fields
import itertools

import httpx
import dask.array
import numpy

from .models import DataSourceStructure
from .query_registration import DictView, query_type_to_name
from .queries import KeyLookup
from .in_memory_catalog import IndexCallable, slice_to_interval


class ClientCatalog(collections.abc.Mapping):

    # This maps the (__module__, __qualname__) sent by the server to a
    # client-side object. It is populated below, in the module scope, so as to
    # reference ClientCatalog itself.
    DEFAULT_DISPATCH = {}

    def __init__(self, client, *, path, metadata, dispatch, queries=None):
        "This is not user-facing. Use ClientCatalog.from_uri."
        self._client = client
        self._metadata = metadata
        self.dispatch = self.DEFAULT_DISPATCH.copy()
        self.dispatch.update(dispatch or {})
        if isinstance(path, str):
            raise ValueError("path is expected to be a list of segments")
        # Stash *immutable* copies just to be safe.
        self._path = tuple(path or [])
        self._queries = tuple(queries or [])
        self._queries_as_params = _queries_to_params(*self._queries)
        self.keys_indexer = IndexCallable(self._keys_indexer)
        self.items_indexer = IndexCallable(self._items_indexer)
        self.values_indexer = IndexCallable(self._values_indexer)

    @classmethod
    def from_uri(cls, uri, dispatch=None):
        client = httpx.Client(base_url=uri.rstrip("/"))
        response = client.get("/metadata/")
        response.raise_for_status()
        metadata = response.json()["data"]["attributes"]["metadata"]
        return cls(client, path=[], metadata=metadata, dispatch=dispatch)

    def __repr__(
        self,
    ):  # TODO When the number of items is very large, show only a subset.
        # That is, do not let repr(self) trigger a large number of HTTP
        # requests paginating through all the results.
        return f"<{type(self).__name__}({set(self)!r})>"

    @property
    def metadata(self):
        "Metadata about this Catalog."
        # Ensure this is immutable (at the top level) to help the user avoid
        # getting the wrong impression that editing this would update anything
        # persistent.
        return DictView(self._metadata)

    def __len__(self):
        response = self._client.get(
            f"/search/{'/'.join(self._path)}",
            params={"fields": "", **self._queries_as_params},
        )
        response.raise_for_status()
        return response.json()["meta"]["count"]

    def __iter__(self):
        next_page_url = f"/search/{'/'.join(self._path)}"
        while next_page_url is not None:
            response = self._client.get(
                next_page_url,
                params={"fields": "", **self._queries_as_params},
            )
            response.raise_for_status()
            for item in response.json()["data"]:
                yield item["id"]
            next_page_url = response.json()["links"]["next"]

    def __getitem__(self, key):
        # Lookup this key *within the search results* of this Catalog.
        response = self._client.get(
            f"/search/{'/'.join(self._path )}",
            params={
                "fields": "metadata",
                **_queries_to_params(KeyLookup(key)),
                **self._queries_as_params,
            },
        )
        response.raise_for_status()
        data = response.json()["data"]
        if not data:
            raise KeyError(key)
        assert (
            len(data) == 1
        ), "The key lookup query must never result more than one result."
        (item,) = data
        dispatch_on = (item["meta"]["__module__"], item["meta"]["__qualname__"])
        cls = self.dispatch[dispatch_on]
        return cls(
            client=self._client,
            path=self._path + (item["id"],),
            metadata=item["attributes"]["metadata"],
            dispatch=self.dispatch,
        )

    def items(self):
        # The base implementation would use __iter__ and __getitem__, making
        # one HTTP request per item. Pull pages instead.
        next_page_url = f"/search/{'/'.join(self._path)}"
        while next_page_url is not None:
            response = self._client.get(
                next_page_url,
                params={"fields": "metadata", **self._queries_as_params},
            )
            response.raise_for_status()
            for item in response.json()["data"]:
                dispatch_on = (item["meta"]["__module__"], item["meta"]["__qualname__"])
                cls = self.dispatch[dispatch_on]
                yield cls(
                    self._client,
                    path=self.path + [item["id"]],
                    metadata=item["attributes"]["metadata"],
                    dispatch=self.dispatch,
                )
            next_page_url = response.json()["links"]["next"]

    def values(self):
        # The base implementation would use __iter__ and __getitem__, making
        # one HTTP request per item. Pull pages instead.
        for _, value in self.items():
            yield value

    def _keys_slice(self, start, stop):
        next_page_url = f"/search/{'/'.join(self._path)}?page[offset]={start}"
        item_counter = itertools.count(start)
        while next_page_url is not None:
            response = self._client.get(
                next_page_url,
                params={"fields": "", **self._queries_as_params},
            )
            response.raise_for_status()
            for item in response.json()["data"]:
                if stop is not None and next(item_counter) == stop:
                    break
                yield item["id"]
            next_page_url = response.json()["links"]["next"]

    def _items_slice(self, start, stop):
        next_page_url = f"/search/{'/'.join(self._path)}?page[offset]={start}"
        item_counter = itertools.count(start)
        while next_page_url is not None:
            response = self._client.get(
                next_page_url,
                params={"fields": "metadata", **self._queries_as_params},
            )
            response.raise_for_status()
            for item in response.json()["data"]:
                key = item["id"]
                dispatch_on = (item["meta"]["__module__"], item["meta"]["__qualname__"])
                cls = self.dispatch[dispatch_on]
                if stop is not None and next(item_counter) == stop:
                    break
                yield key, cls(
                    self._client,
                    path=self._path + (item["id"],),
                    metadata=item["attributes"]["metadata"],
                    dispatch=self.dispatch,
                )
            next_page_url = response.json()["links"]["next"]

    def _item_by_index(self, index):
        if index >= len(self):
            raise IndexError(f"index {index} out of range for length {len(self)}")
        url = f"/search/{'/'.join(self._path)}?page[offset]={index}&page[limit]=1"
        response = self._client.get(
            url, params={"fields": "metadata", **self._queries_as_params}
        )
        response.raise_for_status()
        (item,) = response.json()["data"]
        key = item["id"]
        dispatch_on = (item["meta"]["__module__"], item["meta"]["__qualname__"])
        cls = self.dispatch[dispatch_on]
        value = cls(
            self._client,
            path=self._path + (item["id"],),
            metadata=item["attributes"]["metadata"],
            dispatch=self.dispatch,
        )
        return (key, value)

    def search(self, query):
        return type(self)(
            client=self._client,
            path=self._path,
            queries=self._queries + (query,),
            metadata=self._metadata,
            dispatch=self.dispatch,
        )

    def _keys_indexer(self, index):
        if isinstance(index, int):
            key, _value = self._item_by_index(index)
            return key
        elif isinstance(index, slice):
            start, stop = slice_to_interval(index)
            return list(self._keys_slice(start, stop))
        else:
            raise TypeError(f"{index} must be an int or slice, not {type(index)}")

    def _items_indexer(self, index):
        if isinstance(index, int):
            return self._item_by_index(index)
        elif isinstance(index, slice):
            start, stop = slice_to_interval(index)
            return list(self._items_slice(start, stop))
        else:
            raise TypeError(f"{index} must be an int or slice, not {type(index)}")

    def _values_indexer(self, index):
        if isinstance(index, int):
            _key, value = self._item_by_index(index)
        elif isinstance(index, slice):
            start, stop = slice_to_interval(index)
            return [value for _key, value in self._items_slice(start, stop)]
        else:
            raise TypeError(f"{index} must be an int or slice, not {type(index)}")


class ClientArraySource:
    def __init__(self, client, metadata, path, dispatch):
        self._client = client
        self._metadata = metadata
        self._path = path

    @property
    def metadata(self):
        "Metadata about this Catalog."
        # Ensure this is immutable (at the top level) to help the user avoid
        # getting the wrong impression that editing this would update anything
        # persistent.
        return DictView(self._metadata)

    def describe(self):
        response = self._client.get(
            f"/metadata/{'/'.join(self._path)}", params={"fields": "structure"}
        )
        response.raise_for_status()
        result = response.json()["data"]["attributes"]["structure"]
        return DataSourceStructure(**result)

    def _get_block(self, block, dtype, shape):
        """
        Fetch the data for one block in a chunked (dask) array.
        """
        response = self._client.get(
            f"/blob/array/{'/'.join(self._path)}",
            headers={"Accept": "application/octet-stream"},
            params={"block": ",".join(map(str, block))},
        )
        response.raise_for_status()
        return numpy.frombuffer(response.content, dtype=dtype).reshape(shape)

    def read(self):
        structure = self.describe()
        shape = structure.shape
        dtype = structure.dtype.to_numpy_dtype()
        # Build a client-side dask array whose chunks pull from a server-side
        # dask array.
        name = "remote-dask-array-{self._client.base_url!s}{'/'.join(self._path)}"
        chunks = structure.chunks
        # Count the number of blocks along each axis.
        num_blocks = (range(len(n)) for n in chunks)
        # Loop over each block index --- e.g. (0, 0), (0, 1), (0, 2) .... ---
        # and build a dask task encoding the method for fetching its data from
        # the server.
        dask_tasks = {
            (name,)
            + block: (
                self._get_block,
                block,
                dtype,
                tuple(chunks[dim][i] for dim, i in enumerate(block)),
            )
            for block in itertools.product(*num_blocks)
        }
        return dask.array.Array(
            dask=dask_tasks,
            name=name,
            chunks=chunks,
            dtype=dtype,
            shape=shape,
        )


ClientCatalog.DEFAULT_DISPATCH.update(
    {
        ("catalog_server.in_memory_catalog", "Catalog"): ClientCatalog,
        ("catalog_server.datasources", "ArraySource"): ClientArraySource,
    }
)


def _queries_to_params(*queries):
    "Compute GET params from the queries."
    params = {}
    for query in queries:
        name = query_type_to_name[type(query)]
        for field in fields(query):
            params[f"filter[{name}][condition][{field.name}]"] = getattr(
                query, field.name
            )
    return params
