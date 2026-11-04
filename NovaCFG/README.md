# NovaCFG





# deps
python -m pip install grpcio grpcio-tools protobuf

# pull the proto repo
git clone https://github.com/grafana/alloy-remote-config.git vendor/alloy-remote-config

# generate Python code
python -m grpc_tools.protoc \
  -I vendor/alloy-remote-config/api \
  --python_out=./generated \
  --grpc_python_out=./generated \
  $(find vendor/alloy-remote-config/api -name '*.proto')
