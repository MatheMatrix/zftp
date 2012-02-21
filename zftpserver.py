#!/usr/bin/env python
#coding:utf-8

import json
import os
import urlparse
import uuid

from eventlet.green import socket, httplib
from pyftpdlib import ftpserver
from urllib import quote

SWIFT_HOST = '192.168.56.186'
SWIFT_PORT = '8080'
ACCOUNTS = {'account':'test', 'user':'tester', 'password':'testing'}
ACCOUNT_ROOT_PATH = '/var/www/zftp/'

SINGLE = {'name':None, 'size':None}

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
        path = '%s/%s/%s' % (self.parse.path, quote(container), quote(str(obj)))
        conn = httplib.HTTPConnection('%s:%s'%(SWIFT_HOST, SWIFT_PORT))
        conn.request('DELETE', path, '', {'X-Auth-Token': self.x_auth_token})
        resp = conn.getresponse()
        resp.read()
        if resp.status < 200 or resp.status >= 300:
            print 'delete_object, error, container:%s, obj:%s' % (container, obj)
        print 'delete_object, ok'

sproxy = Swift_Proxy()
sproxy.login()

def path2container(path):
    '''从给出的path得到对应的container '''
    _prefix = len(os.path.join(ACCOUNT_ROOT_PATH, ACCOUNTS['account']))
    return path[_prefix:].lstrip('/').split('/')[0]

def filepath2single(path):
    '''
        从给出的path得到对应的在swift中存放用的uuid，objname
    '''
    try:
        with open(path) as f:
            return json.load(f)
    except ValueError, msg:
        print 'path:%s, its json was broken.'%(path)
        #raise OSError('load json error, %s'%(msg))
    return SINGLE

def filepath2uuid(path):
    return filepath2single(path)['name']

class Fake_Fd():
        '''把和swift的连接封装成一个类fd，让ftplib来使用，
        实现write(), read(), closed(), fileno(), name,  '''
        def __init__(self, filepath, mode):
            self.single = SINGLE
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

            path = os.path.join(sproxy.parse.path, self.container, self.single['name'])
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
            print 'send chunk'
            if chunk is None:
                self.conn.send("0\r\n\r\n")
            self.conn.send('%x\r\n%s\r\n'%(len(chunk), chunk))
            self._size = len(chunk)

        def read(self, buffer_size):
            print 'read chunk:', buffer_size
            return self.r1.read(buffer_size)

        def close(self):
            '''关闭的时候，在本地创建一个文件，把文件的属性标记上'''
            print 'close conn'
            if self.is_write:
                self.conn.send("0\r\n\r\n")
                r1 = self.conn.getresponse()
                #print r1.msg, r1.status, r1.reason
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
        self.log = self.cmd_channel.log

    def open(self, filename, model):
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

    def stat(self, path):
        return self.lstat(path)

    def lstat(self, path):
        statinfo = os.stat(path)
        keys = [x for x in dir(statinfo) if x.startswith('st_')]
        fake_stat = lambda x:x
        for i in keys:
            setattr(fake_stat, i, getattr(statinfo, i))
        if os.path.isfile(path):
            fake_stat.st_size = self.getsize(path)
        return fake_stat
  
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
