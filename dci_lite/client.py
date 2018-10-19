import dateutil.parser

from dciclient.v1.api import context as dci_context

import os
import json


class DCILiteNotFound(Exception):
    pass


class DCILiteDeleteFailure(Exception):
    pass


HTTP_TIMEOUT = 600


def kwargs_to_data(kwargs):
    # Handle the FK
    data = {}
    for i in kwargs:
        if hasattr(kwargs[i], 'id'):
            data[i + '_id'] = kwargs[i].id
        else:
            data[i] = kwargs[i]
    return data


class DCIResource():
    def __init__(self, transport, resource, data,
                 parent_resource=None, subresource=None):
        self._transport = transport
        self._resource = resource
        self._parent_resource = parent_resource
        self._subresource = subresource
        self._uri = self._build_uri()
        self._data = {}
        self._fk = {}
        self._new_data = {}
        self._load_data(data)

    @classmethod
    def from_id(cls, transport, resource, item_id, **kwargs):
        uri = '%s/%s/%s' % (transport.dci_cs_api, resource, item_id)
        r = transport.get(uri, timeout=HTTP_TIMEOUT, params=kwargs)
        if r.status_code == 404:
            msg = 'resource not found at %s: %s' % (uri, r.text)
            raise DCILiteNotFound(msg)
        if r.status_code != 200:
            raise(Exception('Failed to get resource %s: %s' % (uri, r.text)))
        obj = cls(transport, resource, list(r.json().values())[0])
        return obj

    def _build_uri(self):
        if self._subresource:
            return '%s/%s/%s/%s' % (
                self._transport.dci_cs_api,
                self._resource,
                self._parent_resource.id,
                self._subresource)
        else:
            return '%s/%s' % (self._transport.dci_cs_api, self._resource)

    def refresh(self):
        uri = '%s/%s' % (self._uri, self.id)
        r = self._transport.get(uri)
        self._load_data(list(r.json().values())[0])

    def commit(self):
        """Update a specific resource"""
        if not self._new_data:
            return

        uri = self._uri + '/' + self._data['id']
        r = self._transport.put(uri, timeout=HTTP_TIMEOUT,
                                headers={'If-match': self._data['etag']},
                                json=self._new_data)
        if r.status_code != 200:
            msg = 'Failed to commit object %s: %s' % (uri, r.text)
            raise(Exception(msg))
        self._load_data(list(r.json().values())[0])

    def _load_data(self, data):
        for i in data:
            if i in ('created_at', 'updated_at'):
                self._data[i] = dateutil.parser.parse(data[i])
            else:
                self._data[i] = data[i]
        self._new_data = {}

    def download(self, target):
        uri = self._uri + '/' + self._data['id'] + '/content'
        r = self._transport.get(
            uri,
            stream=True,
            timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        with open(target + '.part', 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
        os.rename(target + '.part', target)
        return r

    def __str__(self):
        return "resource %s\ndata: %s\nuncommited data: %s" % (
            self._uri, self._data, self._new_data)

    def __getattr__(self, name):
        def return_func(name, params={}):
            if name + '_id' in self.__dict__['_data']:
                guessed_resource_name = name + 's'
                # NOTE: We should be able to just create a new DCIResource
                # instance here.
                if name not in self.__dict__['_fk']:
                    self.__dict__['_fk'][name] = DCIResource.from_id(
                        self._transport,
                        resource=guessed_resource_name,
                        item_id=self.__dict__['_data'][name + '_id'])
                return self.__dict__['_fk'][name]
            else:
                if name not in self.__dict__['_fk']:
                    self.__dict__['_fk'][name] = DCIResourceCollection(
                        self._transport,
                        self._resource,
                        parent_resource=self,
                        subresource=name)
                return self.__dict__['_fk'][name]

        if name.startswith('_'):
            return self.__dict__[name]
        elif name in self.__dict__['_data']:
            return self.__dict__['_data'][name]
        return return_func(name)

    def __setattr__(self, name, value):
        if name.startswith('_'):
            self.__dict__[name] = value
            return
        self._data[name] = value
        self._new_data[name] = value

    def delete(self):
        uri = self._uri + '/' + self.id
        r = self._transport.delete(
            uri, timeout=HTTP_TIMEOUT,
            headers={'If-match': self._data['etag']})
        if r.status_code != 204:
            raise DCILiteDeleteFailure('failed to delete at %s: %s' % (
                uri, r.text))


class DCIResourceCollection:
    def __init__(self, transport, resource,
                 parent_resource=None, subresource=None):
        self._transport = transport
        self._resource = resource
        self._parent_resource = parent_resource
        self._subresource = subresource
        self._uri = self._build_uri()

    def _build_uri(self):
        if self._subresource:
            return '%s/%s/%s/%s' % (
                self._transport.dci_cs_api,
                self._resource,
                self._parent_resource.id,
                self._subresource)
        else:
            return '%s/%s' % (
                self._transport.dci_cs_api,
                self._resource)

    def add(self, **kwargs):
        uri = self._uri

        if 'data' in kwargs and hasattr(kwargs['data'], 'read'):
            # Stream mode
            r = self._transport.post(
                uri,
                timeout=HTTP_TIMEOUT,
                data=kwargs['data'])
        else:
            data = kwargs_to_data(kwargs)
            r = self._transport.post(
                uri,
                timeout=HTTP_TIMEOUT,
                json=data)
        new_entry = list(r.json().values())[0]
        if r.status_code != 201:
            raise(Exception('Failed to add %s: %s' % (uri, r.text)))
        if not isinstance(new_entry, dict):
            # probably a new jointure entry, we don't return
            # anything
            pass
        elif self._subresource:
            return DCIResource(
                self._transport,
                self._resource,
                new_entry,
                parent_resource=self._parent_resource,
                subresource=self._subresource)
        else:
            return DCIResource(
                self._transport,
                self._resource,
                new_entry,)

    def __iter__(self):
        return self.list()

    def __getitem__(self, item_id):
        return self.get(item_id)

    def __delitem__(self, item):
        self.delete(item)

    def get(self, item_id, **kwargs):
        uri = self._uri + '/' + item_id
        r = self._transport.get(
            uri,
            timeout=HTTP_TIMEOUT,
            params=kwargs)
        if r.status_code == 404:
            msg = 'resource not found at %s: %s' % (uri, r.text)
            raise DCILiteNotFound(msg)
        if r.status_code != 200:
            msg = 'Failed to get resource %s: %s' % (uri, r.text)
            raise(Exception(msg))
        obj = DCIResource(
            self._transport,
            self._resource,
            list(r.json().values())[0])
        return obj

    def delete(self, item):
        uri = self._uri + '/' + item.id
        r = self._transport.delete(
            uri,
            timeout=HTTP_TIMEOUT,
            headers={'If-match': item.etag})
        if r.status_code != 204:
            raise DCILiteDeleteFailure('failed to delete at %s: %s' % (
                uri, r.text))
        return r

    def first(self, **kwargs):
        gen = self.list(**kwargs)
        return next(gen)

    def find_or_add(self, **kwargs):
        try:
            return self.first(where='name:%s' % kwargs['name'])
        except StopIteration:
            return self.add(**kwargs)

    def len(self, **kwargs):
        """List all resources"""
        uri = self._uri
        data = kwargs_to_data(kwargs)
        data['limit'] = 1

        r = self._transport.get(
            uri,
            timeout=HTTP_TIMEOUT,
            params=data)
        if r.status_code == 404:
            msg = 'Resource not found at %s: %s' % (uri, r.text)
            raise DCILiteNotFound(msg)
        try:
            j = r.json()
        except (ValueError):
            msg = 'Invalid answer from server for %s: %s' % (uri, r.text)
            raise Exception(msg)
        return j['_meta']['count']

    def count(self, **kwargs):
        return self.len(**kwargs)

    def list(self, **kwargs):
        """List all resources"""
        uri = self._uri
        data = kwargs_to_data(kwargs)
        data['limit'] = data.get('limit', 1000)

        # Type of resource that we will loop
        resource_type = self._subresource or self._resource

        data['offset'] = 0
        while True:
            r = self._transport.get(
                uri,
                timeout=HTTP_TIMEOUT,
                params=data)
            if r.status_code == 404:
                msg = 'resource not found at %s: %s' % (uri, r.text)
                raise DCILiteNotFound(msg)
            try:
                j = r.json()
                del j['_meta']
            except (KeyError, ValueError):
                msg = 'Invalid answer from server for %s: %s' % (uri, r.text)
                raise Exception(msg)
            items = list(j.values())[0]
            if not len(items):
                break
            for i in items:
                yield DCIResource(self._transport, resource_type, i)
            data['offset'] += data['limit']

    # generic method to handle the POST call
    def __getattr__(self, name):
        def return_func(**kwargs):
            uri = self._uri + '/' + name
            r = self._transport.post(
                uri,
                timeout=HTTP_TIMEOUT,
                data=json.dumps(kwargs_to_data(kwargs)))
            if not r.ok:
                raise(Exception('Failed to call %s: %s' % (uri, r.text)))
            try:
                return DCIResource(
                    self._transport,
                    self._resource,
                    list(r.json().values())[0])
            except ValueError:
                pass
        return return_func


class Transport:
    def __init__(self, context):
        self._context = context
        self._session = context.session
        self.dci_cs_api = self._context.dci_cs_api

    def put(self, uri, **kargs):
        r = self._session.put(uri, **kargs)
        return r

    def post(self, uri, **kargs):
        r = self._session.post(uri, **kargs)
        return r

    def delete(self, uri, **kargs):
        r = self._session.delete(uri, **kargs)
        return r

    def get(self, uri, **kargs):
        r = self._session.get(uri, **kargs)
        return r


class DCIClient:

    def __init__(self, transport):
        self._transport = transport

    @classmethod
    def for_user(cls, dci_login=None, dci_password=None, dci_cs_url=None):
        context = dci_context.build_dci_context(
            dci_login=dci_login,
            dci_password=dci_password,
            dci_cs_url=dci_cs_url)
        return cls(Transport(context))

    @classmethod
    def for_remoteci(cls, dci_client_id=None, dci_api_secret=None,
                     dci_cs_url=None):
        context = dci_context.build_signature_context(
            dci_cs_url=dci_cs_url,
            dci_client_id=dci_client_id,
            dci_api_secret=dci_api_secret)
        return cls(Transport(context))

    def __getattr__(self, resource):
        def return_collection(params={}):
            return DCIResourceCollection(self._transport, resource)
        return return_collection(resource)
