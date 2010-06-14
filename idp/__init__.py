from django.template import RequestContext
from django.shortcuts import render_to_response
from registration.signals import user_registered
from registration.signals import user_activated
from signals import auth_login
from signals import auth_logout
from django.conf import settings
from admin_log_view.models import info

REGISTERED_SERVICE_LIST = []

def register_service_list(list_or_callable):
    '''Register a list of tuple (uri, name) to present in user service list, or
       a callable which will receive the request object and return a list of tuples.
    '''
    REGISTERED_SERVICE_LIST.append(list_or_callable)

def service_list(request):
    '''Compute the service list to show on user homepage'''
    list = []
    for list_or_callable in REGISTERED_SERVICE_LIST:
        if callable(list_or_callable):
            list += list_or_callable(request)
        else:
            list += list_or_callable
    return list

def homepage(request):
    '''Homepage of the IdP'''
    return render_to_response('index.html',
            { 'authorized_services' : service_list(request) },
            RequestContext(request))

def LogRegistered(sender, user, **kwargs):
    msg = user.username + ' is now registered'
    info(msg)

def LogActivated(sender, user, **kwargs):
    msg = user.username + ' has activated his acount'
    info(msg)

def LogRegisteredOI(sender, openid, **kwargs):
    msg = openid 
    msg = str(msg) + ' is now registered'
    info(msg)

def LogAssociatedOI(sender, user, openid, **kwargs):
    msg = user.username + ' has associated is user with ' + openid
    info(msg)

def LogAuthLogin(sender, user, successful, **kwargs):
    if successful:
        msg = user.username + ' has login with success'
    else:    
        msg = user + ' try to login without success'
    info(msg)

def LogAuthLogout(sender, user, **kwargs):
    msg = str(user) 
    msg += ' has logout'
    info(msg)

user_registered.connect(LogRegistered, dispatch_uid = "authentic.idp")
user_activated.connect(LogActivated, dispatch_uid = "authentic.idp")
auth_login.connect(LogAuthLogin, dispatch_uid = "authentic.idp")
auth_logout.connect(LogAuthLogout, dispatch_uid = "authentic.idp")

if settings.AUTH_OPENID:
    from django_authopenid.signals import oid_register
    from django_authopenid.signals import oid_associate
    oid_register.connect(LogRegisteredOI, dispatch_uid = "authentic.idp")
    oid_associate.connect(LogAssociatedOI, dispatch_uid = "authentic.idp")

