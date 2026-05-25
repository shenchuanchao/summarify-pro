"""Check what Flask template actually renders"""
import app
from flask import template_rendered

def check_template(sender, template, context, **extra):
    print(f"Template: {template.name}")
    print(f"  paypal_client_id: {context.get('paypal_client_id', 'N/A')}")
    print(f"  paypal_plan_id:   {context.get('paypal_plan_id', 'N/A')}")
    print(f"  paypal_mode:      {context.get('paypal_mode', 'N/A')}")

template_rendered.connect(check_template, app.app)

with app.app.test_client() as c:
    r = c.get("/")
    # Find the PAYPAL vars in rendered output
    html = r.data.decode("utf-8")
    for line in html.split("\n"):
        if "window.PAYPAL" in line:
            print(f"\nRendered: {line.strip()}")
            break