# Third-party format definitions

`proto/carrier_list_pb2.py` and `proto/carrier_settings_pb2.py` are generated from:

- `platform/tools/carrier_settings/proto/carrier_list.proto`
- `platform/tools/carrier_settings/proto/carrier_settings.proto`

Source: <https://android.googlesource.com/platform/tools/carrier_settings/>

The source `.proto` files are Copyright Google LLC and licensed under the Apache License 2.0. The generated files are kept only to decode the public CarrierSettings protobuf format; no code from the archived GrapheneOS extractor or unlicensed third-party Pixel extractor is copied into this project.

Pixel factory images and the configuration data inside them remain subject to the terms published by Google for the device software. Original factory ZIPs, partition images and extracted MBN files are build inputs under `data/` and are not redistributed by this repository.
