create an upstream backend http


create consumer group (ConfigAccess)  
```sh
export TEAM_NAME=ConfigAccess
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumer_groups/${TEAM_NAME}" -H 'X-API-KEY: edd1c9f034335f136f87ad84b625c8f1' -d '{"plugins": {} }'
```

create consumer group (MimirAccess)
```sh
export TEAM_NAME=MimirAccess
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumer_groups/${TEAM_NAME}" -H 'X-API-KEY: edd1c9f034335f136f87ad84b625c8f1' -d '{"plugins": {} }'
```

create consumer group (LokiAccess)
```sh
export TEAM_NAME=LokiAccess
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumer_groups/${TEAM_NAME}" -H 'X-API-KEY: edd1c9f034335f136f87ad84b625c8f1' -d '{"plugins": {} }'
```

create consumer group (TempoAccess)
```sh
export TEAM_NAME=TempoAccess
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumer_groups/${TEAM_NAME}" -H 'X-API-KEY: edd1c9f034335f136f87ad84b625c8f1' -d '{"plugins": {} }'
```


Create Consumer in APISIX
CONFIG
```sh
export CONSUMER_USER="8386e989c2c8448ea43b977361955190"
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumers/${CONSUMER_USER}_config" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  -d "{
    \"username\": \"${CONSUMER_USER}_config\",
    \"group_id\": \"ConfigAccess\",
    \"plugins\": {
      \"basic-auth\": {
        \"username\": \"${CONSUMER_USER}-config\",
        \"password\": \"password\"
      }
    }
  }"
```

Mimir
```sh
export CONSUMER_USER="c8be86eefeb240bb95e715cc08f0c4e9"
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumers/${CONSUMER_USER}_mimir" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  -d "{
    \"username\": \"${CONSUMER_USER}_mimir\",
    \"group_id\": \"MimirAccess\",
    \"plugins\": {
      \"basic-auth\": {
        \"username\": \"${CONSUMER_USER}-mimir\",
        \"password\": \"password\"
      }
    }
  }"
```

Loki
```sh
export CONSUMER_USER="c8be86eefeb240bb95e715cc08f0c4e9"
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumers/${CONSUMER_USER}_loki" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  -d "{
    \"username\": \"${CONSUMER_USER}_loki\",
    \"group_id\": \"LokiAccess\",
    \"plugins\": {
      \"basic-auth\": {
        \"username\": \"${CONSUMER_USER}-loki\",
        \"password\": \"password\"
      }
    }
  }"
```

Tempo
```sh
export CONSUMER_USER="c8be86eefeb240bb95e715cc08f0c4e9"
curl -X PUT "http://127.0.0.1:9180/apisix/admin/consumers/${CONSUMER_USER}_tempo" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  -d "{
    \"username\": \"${CONSUMER_USER}_tempo\",
    \"group_id\": \"TempoAccess\",
    \"plugins\": {
      \"basic-auth\": {
        \"username\": \"${CONSUMER_USER}-tempo\",
        \"password\": \"password\"
      }
    }
  }"
```

create route
```sh
curl -i -X PUT "http://127.0.0.1:9180/apisix/admin/routes/r-local-980-grpc" \
  -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" \
  -H "Content-Type: application/json" \
  -d '{ 
    "name": "local-980-grpc", 
    "uri": "/*", 
    "upstream_id": "00000000000000000059",
    "basic-auth": {},
    "consumer-restriction": {
        "type": "consumer_group_id",
        "whitelist": ["ConfigAccess"],
        "rejected_code": 403,
        "rejected_msg": "Forbidden for this route"
      }
    }'
```

```sh
curl -i -X PUT "http://127.0.0.1:9180/apisix/admin/routes/r-local-980-grpc" -H "X-API-KEY: edd1c9f034335f136f87ad84b625c8f1" -H "Content-Type: application/json" -d '{ "name": "local-980-grpc", "uri": "/*", "upstream_id": "00000000000000000059"}'
```