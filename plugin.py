import os
import requests
from flask import Flask, request, jsonify

NETBOX = os.environ["NETBOX_URL"].rstrip("/")
HEADERS = {
    "Authorization": f"Token {os.environ['NETBOX_TOKEN']}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
TAG = [{"slug": "docker"}]
GW_TYPE = "com.docker.network.gateway"

HOSTS = {}
for item in os.environ.get("ALLOWED_HOSTS", "").split(","):
    if "=" in item:
        ip, name = item.split("=", 1)
        HOSTS[ip.strip()] = name.strip()

app = Flask(__name__)

def nb(method, path, **kw):
    r = requests.request(method, f"{NETBOX}/api{path}", headers=HEADERS, timeout=5, **kw)
    if not r.ok:
        raise RuntimeError(f"NetBox {r.status_code} sur {method} {path} : {r.text}")
    return r.json() if r.content else {}

def reply(data=None):
    resp = jsonify(data or {})
    resp.headers["Content-Type"] = "application/vnd.docker.plugins.v1.2+json"
    return resp

def error(msg):
    return reply({"Error": msg})

def client_ip():
    return request.headers.get("X-Real-IP", "").strip()

def client_host():
    return HOSTS.get(client_ip())

@app.before_request
def authorize():
    ip = client_ip()
    if ip not in HOSTS:
        return error(f"Request refused : the address {ip or 'unknown'} is not authorized")

def find_prefix(cidr, vrf_name=None):
    """Looks for the IP prefix in Netbox in a specified vrf"""
    params = {"prefix": cidr}
    if vrf_name:
        vrfs = nb("GET", "/ipam/vrfs/", params={"name": vrf_name})["results"]
        if not vrfs:
            raise ValueError(f"VRF {vrf_name} not found in Netbox")
        params["vrf_id"] = vrfs[0]["id"]
    res = nb("GET", "/ipam/prefixes/", params=params)["results"]
    if not res:
        suffix = f" in the VRF {vrf_name}" if vrf_name else ""
        raise ValueError(f"{cidr} prefix not found in Netbox {suffix}")
    if len(res) > 1:
        raise ValueError(f"{cidr} exists in many VRFs : please add --ipam-opt vrf=<nom> to docker network creation")
    return res[0]


def prefix_from_pool(pool_id):
    """PoolID = '<Netbox prefix id>|<cidr>' (old format : '<cidr>')."""
    if "|" in pool_id:
        return nb("GET", f"/ipam/prefixes/{pool_id.split('|')[0]}/")
    return find_prefix(pool_id)


def vrf_of(prefix):
    return prefix["vrf"]["id"] if prefix["vrf"] else None


def vrf_filter(prefix):
    """Search filter for the vrf of a prefix ("null" = Global table)."""
    vrf = vrf_of(prefix)
    return {"vrf_id": vrf if vrf else "null"}


def gateway_of(prefix):
    res = nb("GET", "/ipam/ip-addresses/", params={
        "parent": prefix["prefix"], "role": "anycast", **vrf_filter(prefix)})["results"]
    if not res:
        raise ValueError(f"No gateway (Anycast role) declared in Netbox {prefix['prefix']}")
    return res[0]["address"].split("/")[0]

@app.post("/Plugin.Activate")
def activate():
    return reply({"Implements": ["IpamDriver"]})


@app.post("/IpamDriver.GetCapabilities")
def capabilities():
    return reply({"RequiresMACAddress": False})


@app.post("/IpamDriver.GetDefaultAddressSpaces")
def address_spaces():
    return reply({"LocalDefaultAddressSpace": "local",
                  "GlobalDefaultAddressSpace": "global"})


@app.post("/IpamDriver.RequestPool")
def request_pool():
    body = request.get_json(force=True)
    cidr = body.get("Pool")
    vrf_name = (body.get("Options") or {}).get("vrf")
    if not cidr:
        return error("Please provide a subnet using --subnet")
    try:
        prefix = find_prefix(cidr, vrf_name)
    except Exception as e:
        return error(str(e))
    return reply({"PoolID": f"{prefix['id']}|{cidr}", "Pool": cidr, "Data": {}})


@app.post("/IpamDriver.ReleasePool")
def release_pool():
    return reply()


@app.post("/IpamDriver.RequestAddress")
def request_address():
    body = request.get_json(force=True)
    wanted = body.get("Address")
    is_gw = (body.get("Options") or {}).get("RequestAddressType") == GW_TYPE
    try:
        prefix = prefix_from_pool(body["PoolID"])
        mask = prefix["prefix"].split("/")[1]

        if is_gw:  # lecture seule : aucune écriture dans NetBox
            gw = gateway_of(prefix)
            if wanted and wanted != gw:
                return error(f"The {prefix['prefix']} gateway in Netbox is {gw}, not {wanted}")
            return reply({"Address": f"{gw}/{mask}", "Data": {}})

        desc = f"Conteneur Docker ({client_host()})"
        if wanted:  # IP imposée avec docker run --ip
            existing = nb("GET", "/ipam/ip-addresses/",
                          params={"address": wanted, **vrf_filter(prefix)})["results"]
            if existing:
                return error(f"{wanted} is already assigned in NetBox")
            nb("POST", "/ipam/ip-addresses/", json={
                "address": f"{wanted}/{mask}", "vrf": vrf_of(prefix),
                "status": "active", "description": desc, "tags": TAG})
            addr = f"{wanted}/{mask}"
        else:  # Next free IP ; NetBox gives it the VRF of the prefix
            ip = nb("POST", f"/ipam/prefixes/{prefix['id']}/available-ips/", json={
                "status": "active", "description": desc, "tags": TAG})
            addr = ip["address"]
    except Exception as e:
        return error(str(e))
    return reply({"Address": addr, "Data": {}})


@app.post("/IpamDriver.ReleaseAddress")
def release_address():
    body = request.get_json(force=True)
    addr = body["Address"]
    owner = f"({client_host()})"
    try:
        prefix = prefix_from_pool(body["PoolID"])
        ips = nb("GET", "/ipam/ip-addresses/", params={
            "address": addr, "tag": "docker", **vrf_filter(prefix)})["results"]
        for ip in ips:
            if ip["description"].endswith(owner):  # Only the proprietary host frees the ip address
                nb("DELETE", f"/ipam/ip-addresses/{ip['id']}/")
    except Exception as e:
        return error(str(e))
    return reply()
