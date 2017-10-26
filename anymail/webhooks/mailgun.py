import json
from datetime import datetime

import hashlib
import hmac
from django.utils.crypto import constant_time_compare
from django.utils.timezone import utc

from .base import AnymailBaseWebhookView
from ..exceptions import AnymailWebhookValidationFailure
from ..inbound import AnymailInboundMessage
from ..signals import inbound, tracking, AnymailInboundEvent, AnymailTrackingEvent, EventType, RejectReason
from ..utils import get_anymail_setting, combine


class MailgunBaseWebhookView(AnymailBaseWebhookView):
    """Base view class for Mailgun webhooks"""

    warn_if_no_basic_auth = False  # because we validate against signature

    api_key = None  # (Declaring class attr allows override by kwargs in View.as_view.)

    def __init__(self, **kwargs):
        api_key = get_anymail_setting('api_key', esp_name=self.esp_name,
                                      kwargs=kwargs, allow_bare=True)
        self.api_key = api_key.encode('ascii')  # hmac.new requires bytes key in python 3
        super(MailgunBaseWebhookView, self).__init__(**kwargs)

    def validate_request(self, request):
        super(MailgunBaseWebhookView, self).validate_request(request)  # first check basic auth if enabled
        try:
            token = request.POST['token']
            timestamp = request.POST['timestamp']
            signature = str(request.POST['signature'])  # force to same type as hexdigest() (for python2)
        except KeyError:
            raise AnymailWebhookValidationFailure("Mailgun webhook called without required security fields")
        expected_signature = hmac.new(key=self.api_key, msg='{}{}'.format(timestamp, token).encode('ascii'),
                                      digestmod=hashlib.sha256).hexdigest()
        if not constant_time_compare(signature, expected_signature):
            raise AnymailWebhookValidationFailure("Mailgun webhook called with incorrect signature")


class MailgunTrackingWebhookView(MailgunBaseWebhookView):
    """Handler for Mailgun delivery and engagement tracking webhooks"""

    signal = tracking

    event_types = {
        # Map Mailgun event: Anymail normalized type
        'delivered': EventType.DELIVERED,
        'dropped': EventType.REJECTED,
        'bounced': EventType.BOUNCED,
        'complained': EventType.COMPLAINED,
        'unsubscribed': EventType.UNSUBSCRIBED,
        'opened': EventType.OPENED,
        'clicked': EventType.CLICKED,
        # Mailgun does not send events corresponding to QUEUED or DEFERRED
    }

    reject_reasons = {
        # Map Mailgun (SMTP) error codes to Anymail normalized reject_reason.
        # By default, we will treat anything 400-599 as REJECT_BOUNCED
        # so only exceptions are listed here.
        499: RejectReason.TIMED_OUT,  # unable to connect to MX (also covers invalid recipients)
        # These 6xx codes appear to be Mailgun extensions to SMTP
        # (and don't seem to be documented anywhere):
        605: RejectReason.BOUNCED,  # previous bounce
        607: RejectReason.SPAM,  # previous spam complaint
    }

    def parse_events(self, request):
        return [self.esp_to_anymail_event(request.POST)]

    def esp_to_anymail_event(self, esp_event):
        # esp_event is a Django QueryDict (from request.POST),
        # which has multi-valued fields, but is *not* case-insensitive

        event_type = self.event_types.get(esp_event['event'], EventType.UNKNOWN)
        timestamp = datetime.fromtimestamp(int(esp_event['timestamp']), tz=utc)
        # Message-Id is not documented for every event, but seems to always be included.
        # (It's sometimes spelled as 'message-id', lowercase, and missing the <angle-brackets>.)
        message_id = esp_event.get('Message-Id', esp_event.get('message-id', None))
        if message_id and not message_id.startswith('<'):
            message_id = "<{}>".format(message_id)

        description = esp_event.get('description', None)
        mta_response = esp_event.get('error', esp_event.get('notification', None))
        reject_reason = None
        try:
            mta_status = int(esp_event['code'])
        except (KeyError, TypeError):
            pass
        except ValueError:
            # RFC-3463 extended SMTP status code (class.subject.detail, where class is "2", "4" or "5")
            try:
                status_class = esp_event['code'].split('.')[0]
            except (TypeError, IndexError):
                # illegal SMTP status code format
                pass
            else:
                reject_reason = RejectReason.BOUNCED if status_class in ("4", "5") else RejectReason.OTHER
        else:
            reject_reason = self.reject_reasons.get(
                mta_status,
                RejectReason.BOUNCED if 400 <= mta_status < 600
                else RejectReason.OTHER)

        # Mailgun merges metadata fields with the other event fields.
        # However, it also includes the original message headers,
        # which have the metadata separately as X-Mailgun-Variables.
        try:
            headers = json.loads(esp_event['message-headers'])
        except (KeyError, ):
            metadata = {}
        else:
            variables = [value for [field, value] in headers
                         if field == 'X-Mailgun-Variables']
            if len(variables) >= 1:
                # Each X-Mailgun-Variables value is JSON. Parse and merge them all into single dict:
                metadata = combine(*[json.loads(value) for value in variables])
            else:
                metadata = {}

        # tags are sometimes delivered as X-Mailgun-Tag fields, sometimes as tag
        tags = esp_event.getlist('tag', esp_event.getlist('X-Mailgun-Tag', []))

        return AnymailTrackingEvent(
            event_type=event_type,
            timestamp=timestamp,
            message_id=message_id,
            event_id=esp_event.get('token', None),
            recipient=esp_event.get('recipient', None),
            reject_reason=reject_reason,
            description=description,
            mta_response=mta_response,
            tags=tags,
            metadata=metadata,
            click_url=esp_event.get('url', None),
            user_agent=esp_event.get('user-agent', None),
            esp_event=esp_event,
        )


class MailgunInboundWebhookView(MailgunBaseWebhookView):
    """Handler for Mailgun inbound (route forward-to-url) webhook"""

    signal = inbound

    def parse_events(self, request):
        return [self.esp_to_anymail_event(request)]

    def esp_to_anymail_event(self, request):
        # Inbound uses the entire Django request as esp_event, because we need POST and FILES.
        # Note that request.POST is case-sensitive (unlike email.message.Message headers).
        esp_event = request
        if 'body-mime' in request.POST:
            # Raw-MIME
            message = AnymailInboundMessage.parse_raw_mime(request.POST['body-mime'])
        else:
            # Fully-parsed
            message = self.message_from_mailgun_parsed(request)

        message.envelope_sender = request.POST.get('sender', None)
        message.envelope_recipient = request.POST.get('recipient', None)
        message.stripped_text = request.POST.get('stripped-text', None)
        message.stripped_html = request.POST.get('stripped-html', None)

        message.spam_detected = message.get('X-Mailgun-Sflag', 'No').lower() == 'yes'
        try:
            message.spam_score = float(message['X-Mailgun-Sscore'])
        except (TypeError, ValueError):
            pass

        return AnymailInboundEvent(
            event_type=EventType.INBOUND,
            timestamp=datetime.fromtimestamp(int(request.POST['timestamp']), tz=utc),
            event_id=request.POST.get('token', None),
            esp_event=esp_event,
            message=message,
        )

    def message_from_mailgun_parsed(self, request):
        """Construct a Message from Mailgun's "fully-parsed" fields"""
        # Mailgun transcodes all fields to UTF-8 for "fully parsed" messages
        try:
            attachment_count = int(request.POST['attachment-count'])
        except (KeyError, TypeError):
            attachments = None
        else:
            # Load attachments from posted files: Mailgun file field names are 1-based
            att_ids = ['attachment-%d' % i for i in range(1, attachment_count+1)]
            att_cids = {  # filename: content-id (invert content-id-map)
                att_id: cid for cid, att_id
                in json.loads(request.POST.get('content-id-map', '{}')).items()
            }
            attachments = [
                AnymailInboundMessage.construct_attachment_from_uploaded_file(
                    request.FILES[att_id], content_id=att_cids.get(att_id, None))
                for att_id in att_ids
            ]

        return AnymailInboundMessage.construct(
            headers=json.loads(request.POST['message-headers']),  # includes From, To, Cc, Subject, etc.
            text=request.POST.get('body-plain', None),
            html=request.POST.get('body-html', None),
            attachments=attachments,
        )
