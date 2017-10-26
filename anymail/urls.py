from django.conf.urls import url

from .webhooks.mailgun import MailgunInboundWebhookView, MailgunTrackingWebhookView
from .webhooks.mailjet import MailjetTrackingWebhookView
from .webhooks.mandrill import MandrillTrackingWebhookView
from .webhooks.postmark import PostmarkTrackingWebhookView
from .webhooks.sendgrid import SendGridTrackingWebhookView
from .webhooks.sparkpost import SparkPostTrackingWebhookView


app_name = 'anymail'
urlpatterns = [
    url(r'^mailgun/inbound(_mime)?/$', MailgunInboundWebhookView.as_view(), name='mailgun_inbound_webhook'),

    url(r'^mailgun/tracking/$', MailgunTrackingWebhookView.as_view(), name='mailgun_tracking_webhook'),
    url(r'^mailjet/tracking/$', MailjetTrackingWebhookView.as_view(), name='mailjet_tracking_webhook'),
    url(r'^mandrill/tracking/$', MandrillTrackingWebhookView.as_view(), name='mandrill_tracking_webhook'),
    url(r'^postmark/tracking/$', PostmarkTrackingWebhookView.as_view(), name='postmark_tracking_webhook'),
    url(r'^sendgrid/tracking/$', SendGridTrackingWebhookView.as_view(), name='sendgrid_tracking_webhook'),
    url(r'^sparkpost/tracking/$', SparkPostTrackingWebhookView.as_view(), name='sparkpost_tracking_webhook'),
]
