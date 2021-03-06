import datetime
import logging
import urllib
import lasso

from django.contrib.auth.views import redirect_to_login
from django.conf.urls.defaults import patterns
from django.http import HttpResponse, HttpResponseForbidden, \
    HttpResponseRedirect
from django.utils.translation import ugettext as _
from django.views.decorators.csrf import csrf_exempt
from django.core.urlresolvers import reverse
from django.contrib.auth.models import User
from django.conf import settings

from authentic2.saml.models import LibertyArtifact
from authentic2.saml.common import get_idff12_metadata, create_idff12_server, \
    load_provider, load_federation, load_session, save_federation, \
    save_session, return_idff12_response, get_idff12_request_message, \
    get_soap_message, return_saml_soap_response
from authentic2.utils import cache_and_validate

def fill_assertion(request, saml_request, assertion, provider_id):
    '''Stuff an assertion with information extracted from the user record
       and from the session, and eventually from transactions linked to the
       request, i.e. a login event or a consent event.'''
    # Use assertion ID as session index
    assertion.authenticationStatement.sessionIndex = assertion.assertionId
    # TODO: add attributes from user account
    # TODO: determine and add attributes from the session, for anonymous
    # users (pseudonymous federation, openid without accoutns)
    # TODO: add information from the login event, of the session or linked
    # to the request id
    # TODO: use information from the consent event to specialize release of
    # attributes (user only authorized to give its email for email)

def build_assertion(request, login):
    '''After a successfully validated authentication request, build an
       authentication assertion'''
    now = datetime.datetime.utcnow()
    # 1 minute ago
    notBefore = now-datetime.timedelta(0,60)
    # 1 minute in the future
    notOnOrAfter = now+datetime.timedelta(0,60)
    # TODO: find authn method from login event or from session
    login.buildAssertion(lasso.LIB_AUTHN_CONTEXT_CLASS_REF_PREVIOUS_SESSION,
            now.isoformat()+'Z',
            'unused', # reauthenticateOnOrAfter is only for ID-FF 1.2
            notBefore.isoformat()+'Z',
            notOnOrAfter.isoformat()+'Z')
    assertion = login.assertion
    fill_assertion(request, login.request, assertion, login.remoteProviderId)

@cache_and_validate(settings.LOCAL_METADATA_CACHE_TIMEOUT)
def metadata(request):
    '''Return ID-FFv1.2 metadata for our IdP'''
    return HttpResponse(get_idff12_metadata(request, reverse(metadata)),
            mimetype = 'text/xml')

def save_artifact(request, login):
    LibertyArtifact(artifact = login.assertionArtifact,
            django_session_key = request.session.session_key,
            provider_id = login.remoteProviderId).save()

# TODO: handle cancellation, by retrieving a login event and looking for
# cancelled flag
# TODO: handle force_authn by redirecting to the login page with a parameter
# linking the login event with this request id and next=current_path
@csrf_exempt
def sso(request):
    """Endpoint for AuthnRequests asynchronously sent, i.e. POST or Redirect"""
    # 1. Process the request, separate POST and GET treatment
    message = get_idff12_request_message(request)
    if not message:
        return HttpResponseForbidden('Invalid SAML 1.1 AuthnRequest: "%s"' % message)
    server = create_idff12_server(request, reverse(metadata))
    login = lasso.Login(server)
    while True:
        try:
            logging.debug('ID-FFv1.2: processing sso request %r' % message)
            login.processAuthnRequestMsg(message)
            break
        except lasso.ProfileInvalidMsgError:
            message = _('Invalid SAML 1.1 AuthnRequest: %r') % message
            logging.error(message)
            return HttpResponseForbidden(message)
        except lasso.DsInvalidSignatureError:
            message = _('Invalid signature on SAML 1.1 AuthnRequest: %r') % message
            logging.error(message)
            # This error is handled through SAML status codes, so return a
            # response
            return finish_sso(request, login)
        except lasso.ServerProviderNotFoundError:
            # This path is not exceptionnal it should be normal since we did
            # not load any provider in the Server object
            provider_id = login.remoteProviderId
            # 2. Lookup the ProviderID
            logging.info('ID-FFv1.2: AuthnRequest from %r' % provider_id)
            provider_loaded = load_provider(request, provider_id, server=login.server)
            if not provider_loaded:
                consent_obtained = False
                message = _('ID-FFv1.2: provider %r unknown') % provider_id
                logging.warning(message)
                return HttpResponseForbidden(message)
            else:
                # XXX: does consent be always automatic for known providers ? Maybe
                # add a configuration key on the provider.
                consent_obtained = True
    return sso_after_process_request(request, login,
            consent_obtained = consent_obtained)

def sso_after_process_request(request, login,
        consent_obtained = True, user = None, save = True):
    '''Common path for sso and idp_initiated_sso.

       consent_obtained: whether the user has given his consent to this federation
       user: the user which must be federated, if None, current user is the default.
       save: whether to save the result of this transaction or not.
    '''
    if user is None:
        user = request.user
    # Flags possible:
    # - consent
    # - isPassive
    # - forceAuthn
    #
    # 3. TODO: Check for permission
    if login.mustAuthenticate():
        # TODO:
        # check that it exists a login transaction for this request id
        #  - if there is, then provoke one with a redirect to
        #  login?next=<current_url>
        #  - if there is then set user_authenticated to the result of the
        #  login event
        # Work around lack of informations returned by mustAuthenticate()
        if login.request.forceAuthn or request.user.is_anonymous():
            return redirect_to_login(request.get_full_path())
        else:
            user_authenticated = True
    else:
        user_authenticated = not request.user.is_anonymous()
    # 3.1 Ask for consent
    if user_authenticated:
        # TODO: for autoloaded providers always ask for consent
        if login.mustAskForConsent() or not consent_obtained:
            # TODO: replace False by check against request id
            if False:
                consent_obtained = True
            # i.e. redirect to /idp/consent?id=requestId
            # then check that Consent(id=requestId) exists in the database
            else:
                return HttpResponseRedirect('consent_federation?id=%s&next=%s' %
                        ( login.request.requestId,
                            urllib.quote(request.get_full_path())) )
    # 4. Validate the request, passing authentication and consent status
    try:
        login.validateRequestMsg(user_authenticated, consent_obtained)
    except:
        raise
        do_federation = False
    else:
        do_federation = True
    # 5. Lookup the federations
    if do_federation:
        load_federation(request, login, user)
        load_session(request, login)
        # 3. Build and assertion, fill attributes
        build_assertion(request, login)
    return finish_sso(request, login, user = user, save = save)

def finish_sso(request, login, user = None, save = True):
    '''Return the response to an AuthnRequest

       user: the user which must be federated, if None default to current user.
       save: whether to save the result of this transaction or not.
    '''
    if user is None:
        user = request.user
    if login.protocolProfile == lasso.LOGIN_PROTOCOL_PROFILE_BRWS_ART:
        login.buildArtifactMsg(lasso.HTTP_METHOD_REDIRECT)
        save_artifact(request, login)
    elif login.protocolProfile == lasso.LOGIN_PROTOCOL_PROFILE_BRWS_POST:
        login.buildAuthnResponseMsg()
    else:
        raise NotImplementedError()
    if save:
        save_federation(request, login)
        save_session(request, login)
    return return_idff12_response(request, login,
            title=_('Authentication response'))

def artifact_resolve(request, soap_message):
    '''Resolve a SAMLv1.1 ArtifactResolve request
    '''
    server = create_idff12_server(request, reverse(metadata))
    login = lasso.Login(server)
    try:
        login.processRequestMsg(soap_message)
    except:
        raise
    logging.debug('ID-FFv1.2 artifact resolve %r' % soap_message)
    liberty_artifact = LibertyArtifact.objects.get(
            artifact = login.assertionArtifact)
    if liberty_artifact:
        liberty_artifact.delete()
        provider_id = liberty_artifact.provider_id
        load_provider(request, provider_id, server=login.server)
        load_session(request, login,
                session_key = liberty_artifact.django_session_key)
        logging.info('ID-FFv1.2 artifact resolve from %r for artifact %r' % (
                        provider_id, login.assertionArtifact))
    else:
         logging.warning('ID-FFv1.2 no artifact found for %r' % login.assertionArtifact)
         provider_id = None
    return finish_artifact_resolve(request, login, provider_id,
            session_key = liberty_artifact.django_session_key)

def finish_artifact_resolve(request, login, provider_id, session_key = None):
    '''Finish artifact resolver processing:
        compute a response, returns it and eventually update stored
        LassoSession.

        provider_id: the entity id of the provider which should receive the artifact
        session_key: the session_key of the session linked to the artifact, if None it means no artifact was found
    '''
    try:
        login.buildResponseMsg(provider_id)
    except:
        raise
    if session_key:
        save_session(request, login,
                session_key = session_key)
    return return_saml_soap_response(login)

@csrf_exempt
def soap(request):
    '''SAMLv1.1 soap endpoint implementation.

       It should handle request for:
        - artifact resolution
        - logout
        - and federation termination'''
    soap_message = get_soap_message(request)
    request_type = lasso.getRequestTypeFromSoapMsg(soap_message)
    if request_type == lasso.REQUEST_TYPE_LOGIN:
        return artifact_resolve(request, soap_message)
    else:
        message = _('ID-FFv1.2: soap request type %r is currently not supported') % request_type
        logging.warning(message)
        return NotImplementedError(message)

def check_delegated_authentication_permission(request):
    return request.user.is_superuser()

def idp_sso(request, provider_id, user_id = None):
    '''Initiate an SSO toward provider_id without a prior AuthnRequest
    '''
    assert provider_id, 'You must call idp_initiated_sso with a provider_id parameter'
    server = create_idff12_server(request, reverse(metadata))
    login = lasso.Login(server)
    liberty_provider = load_provider(request, provider_id, server=login.server)
    service_provider = liberty_provider.service_provider
    binding = service_provider.prefered_assertion_consumer_binding
    nid_policy = service_provider.default_name_id_format
    if user_id:
        user = User.get(id = user_id)
        if not check_delegated_authentication_permission(request):
            logging.warning('ID-FFv1.2: %r tried to log as %r on %r but was forbidden' % (
                                    request.user, user, provider_id))
            return HttpResponseForbidden('You must be superuser to log as another user')
    else:
        user = request.user
    load_federation(request, login, user)
    if not liberty_provider:
        message = _('ID-FFv1.2: provider %r unknown') % provider_id
        logging.warning('ID-FFv1.2: provider %r unknown' % provider_id)
        return HttpResponseForbidden(message)
    login.initIdpInitiatedAuthnRequest(provider_id)
    if binding == 'art':
        login.request.protocolProfile = lasso.LIB_PROTOCOL_PROFILE_BRWS_ART
    elif binding == 'post':
        login.request.protocolProfile = lasso.LIB_PROTOCOL_PROFILE_BRWS_POST
    else:
        raise Exception('Unsupported binding %r' % binding)
    if nid_policy == 'persistent':
        login.request.nameIdPolicy = lasso.LIB_NAMEID_POLICY_TYPE_FEDERATED
    elif nid_policy == 'transient':
        login.request.nameIdPolicy = lasso.LIB_NAMEID_POLICY_TYPE_ONE_TIME
    else:
        message = _('ID-FFv1.2: default nameIdPolicy unsupported %r') % nid_policy
        logging.error(message)
        raise Exception(message)
    login.processAuthnRequestMsg(None)

    return sso_after_process_request(request, login,
            consent_obtained = True, user = user, save = False)

urlpatterns = patterns('',
    (r'^metadata$', metadata),
    (r'^sso$', sso),
    (r'^soap', soap),
    (r'^idp_sso/(.*)$', idp_sso),
    (r'^idp_sso/([^/]*)/([^/]*)$', idp_sso),
)
