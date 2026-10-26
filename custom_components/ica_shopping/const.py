DOMAIN = "ica_shopping"

CONF_ICA_ID = "personal_id"
CONF_ICA_PIN = "pin_code"
CONF_AUTH_STATE = "auth_state"
CONF_ICA_LIST_ID = "ica_list_id"
CONF_REMOVE_STRIKED = "remove_striked"
CONF_TODO_ENTITY_ID = "todo_entity_id"

DEFAULT_SCAN_INTERVAL = 15  # minutes

# Login, same OAuth flow as the ICA app
AUTH_BASE_URL = "https://ims.icagruppen.se"
OAUTH2_AUTHORIZE_ENDPOINT = "oauth/v2/authorize"
OAUTH2_TOKEN_ENDPOINT = "oauth/v2/token"
LOGIN_ENDPOINT = "authn/authenticate/IcaCustomers"
APP_REGISTRATION_ENDPOINT = "register"
APP_REGISTRATION_CLIENT_ID = "ica-app-dcr-registration"
APP_REGISTRATION_CLIENT_SECRET = (
    "uxLHTBvZ-Z2fV-SbrHl1E-tz7vB3jQFrwAdSLlbVMMu1rxDdvJU0s8KGu9d1wLS4"
)
AUTH_REDIRECT_URI = "icacurity://app"
AUTH_ACR = "urn:se:curity:authentication:html-form:IcaCustomers"

# Shopping lists, same API as the ICA app
API_BASE_URL = "https://apimgw-pub.ica.se"
API_LISTS = f"{API_BASE_URL}/sverige/digx/mobile/shoppinglistservice/v1/shoppinglists"
API_LIST = f"{API_LISTS}/{{list_id}}"
API_LIST_SYNC = f"{API_LISTS}/{{list_id}}/sync"
