import awsgi

from app import app as flask_app


def handler(event, context):
    return awsgi.response(
        flask_app,
        event,
        context,
        base64_content_types={
            "application/pdf",
        },
    )
