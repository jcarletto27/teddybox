
set(
    COMPONENT_SRCS "ledman.c" "main.c" "battery.c" "cloud.c" "malloc.c" "opus_memory.c" "nfc.c" "ota.c" "playback.c" "wifi.c" "webserver.c" "accel.c" "proto/protobuf-c.c" "proto/proto/toniebox.pb.taf-header.pb-c.c"
    EMBED_FILES "favicon.ico" "upload_script.html")
    
set(COMPONENT_ADD_INCLUDEDIRS . "proto" "proto/proto")

register_component()

# Keep the codec's large state allocation out of the fragmented runtime heap.
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=opus_multistream_decoder_create"
    "-Wl,--wrap=opus_multistream_decoder_destroy")
