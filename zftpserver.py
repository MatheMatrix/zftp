#!/usr/bin/env python
#coding:utf-8

import json
import os
import hashlib
import urlparse
import uuid

from pyftpdlib import ftpserver
from eventlet.green import socket, httplib
from urllib import quote

SWIFT_HOST = '192.168.56.186'
SWIFT_PORT = '8080'
ACCOUNTS = {'account':'test', 'user':'tester', 'password':'testing'}
ACCOUNT_ROOT_PATH = '/var/www/zftp/'


class Swift_Proxy():
    '''用来和swift通信'''
    def __init__(self):
        self.host = SWIFT_HOST
        self.port = SWIFT_PORT
        self.account = ACCOUNTS['account']
        self.user = ACCOUNTS['user']
        self.password = ACCOUNTS['password']
        self.x_auth_token = ''
        self.x_storage_url = ''
        self.parse = ''

    def login(self):
        headers={"X-Storage-User": self.account+":"+self.user ,  \
                     "X-Storage-Pass":self.password}
        conn = httplib.HTTPConnection('%s:%s'%(SWIFT_HOST, SWIFT_PORT))
        conn.request('GET', '/auth/v1.0', headers=headers)
        resp = conn.getresponse()
        self.x_auth_token = resp.msg["x-auth-token"]
        _url = urlparse.urlparse(resp.msg['x-storage-url'])
        self.x_storage_url = _url.geturl().replace(\
                _url.netloc, '%s:%s'%(self.host, self.port))
        self.parse = urlparse.urlparse(self.x_storage_url) 
        conn.close()
        print 'login resp:', self.x_storage_url, self.x_auth_token

    def put_container(self, container):
        headers={"X-Auth-Token":self.x_auth_token}
        path = os.path.join(self.parse.path, container.lstrip('/'))
        conn = httplib.HTTPConnection('%s:%s'%(SWIFT_HOST, SWIFT_PORT))
        conn.request('PUT', path, headers=headers)
        resp = conn.getresponse()
        print 'put container:', resp.msg, resp.status, resp.reason

    def delete_object(self, container, obj):
        """Wrapper for :func:`delete_object`"""
        path = '%s/%s/%s' % (self.parse.path, quote(container), quote(obj))
        print path
        conn = httplib.HTTPConnection('%s:%s'%(SWIFT_HOST, SWIFT_PORT))
        conn.request('DELETE', path, '', {'X-Auth-Token': self.x_auth_token})
        resp = conn.getresponse()
        resp.read()
        if resp.status < 200 or resp.status >= 300:
            raise OSError('Object DELETE failed', resp.status, 
                   resp.reason, resp.msg, container, obj)
        print 'delete_object, ok'

sproxy = Swift_Proxy()
sproxy.login()

def path2container(path):
    '''从给出的path得到对应的container '''
    _prefix = len(os.path.join(ACCOUNT_ROOT_PATH, ACCOUNTS['account']))
    return path[_prefix:].lstrip('/').split('/')[0]

def filepath2single(path):
    '''从给出的path得到对应的在swift中存放用的uuid，objname '''
    single = None
    with open(path) as f:
        single = json.load(f)
    return single

def filepath2uuid(path):
    '''从给出的path得到对应的在swift中存放用的uuid，objname '''
    return filepath2single(path)['name']

class Fake_Fd():
        '''把和swift的连接封装成一个类fd，让ftplib来使用，
        实现write(), read(), closed(), fileno(), name,  '''
        def __init__(self, filepath, mode):
            self.single = {'name':None, 'size':None}
            self.filepath = filepath
            self.mode = mode
            self.name = os.path.basename(filepath)
            self.is_write = False
            self.closed = False

            self.container = path2container(filepath)

            if os.path.isfile(self.filepath):
                self.single = filepath2single(self.filepath)
            else:
                self.single = {'name':str(uuid.uuid1()), 'size':0}
            print self.single

            path = os.path.join(sproxy.parse.path, self.container, self.single['name'])
            print path
            conn = httplib.HTTPConnection('%s:%s'%(SWIFT_HOST, SWIFT_PORT))

            if self.mode == 'wb':
                print 'conn to for put...'
                conn.putrequest('PUT', quote(path))  
                conn.putheader('Transfer-Encoding', 'chunked')
                conn.putheader('X-Auth-Token', sproxy.x_auth_token)
                conn.endheaders()
                self.conn = conn
                self._size = 0
                self.is_write = True
            else:
                print 'conn to for get...'
                conn.putrequest('GET', quote(path))  
                conn.putheader('X-Auth-Token', sproxy.x_auth_token)
                conn.endheaders()
                self.conn = conn
                self.r1 = self.conn.getresponse()

        def write(self, chunk):
            '''往swift里面写'''
            if chunk is None:
                print 'send chunk zero'
                self.conn.send("0\r\n\r\n")
            print 'send chunk'
            self.conn.send('%x\r\n%s\r\n'%(len(chunk), chunk))
            self._size = len(chunk)

        def read(self, buffer_size):
            print 'read:', buffer_size
            return self.r1.read(buffer_size)

        def close(self):
            '''关闭的时候，在本地创建一个文件，把文件的属性标记上'''
            print 'close conn'
            self.conn.send("0\r\n\r\n")
            if self.is_write:
                r1 = self.conn.getresponse()
                print r1.msg, r1.status, r1.reason
                self.single['size'] = self._size
                with open(self.filepath, self.mode) as f:
                    f.write(json.dumps(self.single))
            self.conn.close()
            self.closed = True

        def fileno(self):
            raise NotImplementedError

class ZAuthorizer(ftpserver.DummyAuthorizer):
    def __init__(self):
        ftpserver.DummyAuthorizer.__init__(self)

    def add_user(self, username, password, homedir, perm='elr',
                    msg_login="Login successful.", msg_quit="Goodbye."):
        '''
        username='username'
        homedir 对应swift中的container，当前只能是1级目录，根据 
        account_root_path + homedir 来合成homedir
        '''
        container = homedir
        account = ACCOUNTS['account']
        homedir = os.path.join(ACCOUNT_ROOT_PATH, account, homedir.lstrip('/'))
        print 'add user homedir:', homedir
        if not os.path.isdir(homedir):
            os.makedirs(homedir)
        sproxy.put_container(container)
        super(ZAuthorizer, self).add_user(username, password, homedir, perm,
                    msg_login, msg_quit)
        self.user_table[username]['container'] = container
        print self.user_table

class Swift_Filesystem(ftpserver.AbstractedFS):
    def __init__(self, root, cmd_channel):
        ftpserver.AbstractedFS.__init__(self, root, cmd_channel)
        self.cwd = '/'

    def open(self, filename, model):
        print filename
        print 'in open:filename:%s, model:%s'%(filename, model)
        return Fake_Fd(filename, model)
    
    def remove(self, path, delete_local=True):
        print 'remove', path
        if os.path.isfile(path):
            sproxy.delete_object(path2container(path), filepath2uuid(path))
        if delete_local:
            super(Swift_Filesystem, self).remove(path)

    def getsize(self, path):
        return filepath2single(path)['size']
    
    def rmdir(self, path):
        """Remove the specified directory."""
        #for p, d, f in os.walk(path):
            #print p, d, f
            #for _f in f:
                #filepath = os.path.join(p, _f)
                #print 'rmdir', _f, filepath
                #self.remove(filepath, delete_local=False)
        super(Swift_Filesystem, self).rmdir(path)

def main():
    zauthorizer = ZAuthorizer()
    test = ['ftptester', 'ftptesting', '/container1', 'elradfmw']
    zauthorizer.add_user(test[0], test[1], test[2], perm=test[3])
    ftp_handler = ftpserver.FTPHandler
    ftp_handler.authorizer = zauthorizer
    ftp_handler.abstracted_fs = Swift_Filesystem
    address = ("127.0.0.1", 1876)
    ftpd = ftpserver.FTPServer(address, ftp_handler)
    print 'please login: user:%s, password:%s' % (test[0], test[1])
    ftpd.serve_forever()

if __name__ == "__main__":
    main()
