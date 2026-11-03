#include "acipc_bridge.h"
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <uuid/uuid.h>
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <string.h>
#include <time.h>

typedef struct {
    uint16_t flags, len;
    uint32_t index;
    uint64_t ext, buffer, metadata;
    uint32_t reserved[8];
} Slot;
_Static_assert(sizeof(Slot) == 64, "slot ABI");
_Static_assert(offsetof(Slot, buffer) == 16, "slot buffer ABI");
typedef struct {
    void *(*attr_create)(void);
    void (*attr_destroy)(void *);
    int (*attr_set)(void *, int, uint64_t);
    void *(*create)(const uuid_t, uint32_t, int, uint32_t, void *);
    void (*destroy)(void *);
    uint32_t (*ring_id)(void *, int);
    void *(*tx_ring)(void *, uint32_t);
    void *(*rx_ring)(void *, uint32_t);
    void *(*next)(void *, void *, Slot *);
    void (*set_slot)(void *, void *, const Slot *);
    int (*advance)(void *, void *);
    int (*sync)(void *, int);
    int (*defunct)(void *);
    int (*get_fd)(void *);
} API;
static API api;
static int api_ready;
typedef struct {
    void *channel, *tx, *rx;
    int fd, fatal;
    pthread_mutex_t lock;
} Transport;
static Transport transports[3] = {
    {.fd = -1, .lock = PTHREAD_MUTEX_INITIALIZER},
    {.fd = -1, .lock = PTHREAD_MUTEX_INITIALIZER},
    {.fd = -1, .lock = PTHREAD_MUTEX_INITIALIZER}
};
static int opened;
static unsigned receive_start;
static const uint8_t rx_types[] = {4, 2, 3};
#ifdef ACIPC_BRIDGE_TEST
static int test_discovery = 1;
#endif

static int api_error(int value) {
    if (value > 0) return -value;
    return -(errno ? errno : EIO);
}
static int load_api(void) {
    if (api_ready) return 0;
#define LOAD(field, symbol) do { \
    *(void **)(&api.field) = dlsym(RTLD_DEFAULT, symbol); \
    if (!api.field) return -ENOSYS; \
} while (0)
    LOAD(attr_create, "os_channel_attr_create");
    LOAD(attr_destroy, "os_channel_attr_destroy");
    LOAD(attr_set, "os_channel_attr_set");
    LOAD(create, "os_channel_create_extended");
    LOAD(destroy, "os_channel_destroy");
    LOAD(ring_id, "os_channel_ring_id");
    LOAD(tx_ring, "os_channel_tx_ring");
    LOAD(rx_ring, "os_channel_rx_ring");
    LOAD(next, "os_channel_get_next_slot");
    LOAD(set_slot, "os_channel_set_slot_properties");
    LOAD(advance, "os_channel_advance_slot");
    LOAD(sync, "os_channel_sync");
    LOAD(defunct, "os_channel_is_defunct");
    LOAD(get_fd, "os_channel_get_fd");
#undef LOAD
    api_ready = 1;
    return 0;
}

static int find_nexuses(uuid_t ids[3]) {
#ifdef ACIPC_BRIDGE_TEST
    if (!test_discovery) return -ENODEV;
    memset(ids, 0, sizeof(uuid_t) * 3);
    for (unsigned i = 0; i < 3; ++i) ids[i][0] = (uint8_t)i;
    return 0;
#else
    const CFStringRef names[] = {CFSTR("hci"), CFSTR("acl"), CFSTR("sco")};
    unsigned found = 0;
    io_iterator_t iterator = 0;
    kern_return_t kr = IOServiceGetMatchingServices(kIOMainPortDefault,
        IOServiceMatching("AppleConvergedIPCRTIInterface"), &iterator);
    if (kr != KERN_SUCCESS) return -EIO;
    int error = 0;
    io_service_t service;
    while ((service = IOIteratorNext(iterator))) {
        CFTypeRef protocol = IORegistryEntryCreateCFProperty(service,
            CFSTR("ACIPCInterfaceProtocol"), kCFAllocatorDefault, 0);
        for (unsigned i = 0; i < 3; ++i) {
            if (!protocol || CFGetTypeID(protocol) != CFStringGetTypeID() ||
                !CFEqual(protocol, names[i])) continue;
            CFTypeRef id = IORegistryEntrySearchCFProperty(service, kIOServicePlane,
                CFSTR("IOSkywalkNexusUUID"), kCFAllocatorDefault,
                kIORegistryIterateRecursively);
            char value[40];
            uuid_t candidate;
            int valid = id && CFGetTypeID(id) == CFStringGetTypeID() &&
                CFStringGetCString(id, value, sizeof(value), kCFStringEncodingUTF8) &&
                uuid_parse(value, candidate) == 0;
            if (id) CFRelease(id);
            if (!valid) error = -EPROTO;
            else if ((found & (1u << i)) && uuid_compare(ids[i], candidate))
                error = -EEXIST;
            else { uuid_copy(ids[i], candidate); found |= 1u << i; }
        }
        if (protocol) CFRelease(protocol);
        IOObjectRelease(service);
    }
    IOObjectRelease(iterator);
    if (error) return error;
    return found == 7 ? 0 : -ENODEV;
#endif
}

/* Exact canonical length, or canonical length plus the observed 8-byte footer.
 * Do not parse the footer or copy it to the caller.
 */
static int packet_length(uint8_t type, const uint8_t *bytes, size_t length,
                         int allow_footer) {
    if (!bytes) return -EINVAL;
    size_t header, payload;
    switch (type) {
    case 1:
    case 3:
        header = 3;
        if (length < header) return -EPROTO;
        payload = bytes[2];
        break;
    case 2:
        header = 4;
        if (length < header) return -EPROTO;
        payload = bytes[2] | ((size_t)bytes[3] << 8);
        break;
    case 4:
        header = 2;
        if (length < header) return -EPROTO;
        payload = bytes[1];
        break;
    default: return -EINVAL;
    }
    size_t canonical = header + payload;
    if (length != canonical && (!allow_footer || length != canonical + 8))
        return -EPROTO;
    return (int)canonical;
}

void acipc_close(void) {
    opened = 0;
    for (unsigned i = 0; i < 3; ++i) {
        Transport *t = &transports[i];
        if (t->channel) api.destroy(t->channel);
        t->channel = t->tx = t->rx = NULL;
        t->fd = -1;
        t->fatal = 0;
    }
    receive_start = 0;
}

int acipc_open(void) {
    if (opened) return -EALREADY;
    int error = load_api();
    if (error) return error;
    uuid_t ids[3];
    error = find_nexuses(ids);
    if (error) return error;
    errno = 0;
    void *attr = api.attr_create();
    if (!attr) return api_error(-1);
    errno = 0;
    int rc = api.attr_set(attr, 6, 0); /* EXCLUSIVE=0 */
    if (!rc) { errno = 0; rc = api.attr_set(attr, 7, 1); } /* NO_AUTO_SYNC=1 */
    if (rc) { error = api_error(rc); goto finish; }
    for (unsigned i = 0; i < 3; ++i) {
        Transport *t = &transports[i];
        errno = 0;
        t->channel = api.create(ids[i], 0, 0, UINT32_MAX, attr); /* TX_RX */
        if (!t->channel) { error = api_error(-1); goto finish; }
        t->tx = api.tx_ring(t->channel, api.ring_id(t->channel, 0));
        t->rx = api.rx_ring(t->channel, api.ring_id(t->channel, 2));
        t->fd = api.get_fd(t->channel);
        if (!t->tx || !t->rx || t->fd < 0) { error = -EIO; goto finish; }
    }
    /* Check all channels after the final channel is open. Do not consume RX. */
    for (unsigned i = 0; i < 3; ++i) {
        Transport *t = &transports[i];
        if (api.defunct(t->channel)) { error = -ENXIO; goto finish; }
        errno = 0;
        rc = api.sync(t->channel, 1);
        if (rc) { error = api_error(rc); goto finish; }
        Slot pending = {0};
        if (api.next(t->rx, NULL, &pending)) { error = -EBUSY; goto finish; }
    }
finish:
    api.attr_destroy(attr);
    if (error) acipc_close();
    else opened = 1;
    return error;
}

int acipc_send(uint8_t type, const uint8_t *bytes, size_t length) {
    if (type < 1 || type > 3) return -EINVAL;
    int canonical = packet_length(type, bytes, length, 0);
    if (canonical < 0) return canonical;
    if (!opened) return -ENOTCONN;
    Transport *t = &transports[type - 1];
    pthread_mutex_lock(&t->lock);
    int error = t->fatal;
    if (error) goto finish;
    if (api.defunct(t->channel)) { error = t->fatal = -ENXIO; goto finish; }
    errno = 0;
    int rc = api.sync(t->channel, 0);
    if (rc) { error = t->fatal = api_error(rc); goto finish; }
    Slot properties = {0};
    void *slot = api.next(t->tx, NULL, &properties);
    if (!slot) { error = -EAGAIN; goto finish; }
    if (!properties.buffer) { error = t->fatal = -EPROTO; goto finish; }
    if (length > properties.len) { error = -EMSGSIZE; goto finish; }
    memcpy((void *)(uintptr_t)properties.buffer, bytes, length);
    properties.len = (uint16_t)length;
    api.set_slot(t->tx, slot, &properties);
    errno = 0;
    rc = api.advance(t->tx, slot);
    if (!rc) { errno = 0; rc = api.sync(t->channel, 0); }
    if (rc) error = t->fatal = api_error(rc);
finish:
    pthread_mutex_unlock(&t->lock);
    return error;
}

static int receive_one(unsigned index, uint8_t *type, uint8_t *bytes,
                       size_t capacity) {
    Transport *t = &transports[index];
    pthread_mutex_lock(&t->lock);
    int result = t->fatal;
    if (result) goto finish;
    if (api.defunct(t->channel)) { result = t->fatal = -ENXIO; goto finish; }
    errno = 0;
    int rc = api.sync(t->channel, 1);
    if (rc) { result = t->fatal = api_error(rc); goto finish; }
    Slot properties = {0};
    void *slot = api.next(t->rx, NULL, &properties);
    if (!slot) goto finish;
    result = packet_length(rx_types[index],
        (const uint8_t *)(uintptr_t)properties.buffer, properties.len, 1);
    if (result < 0) { result = t->fatal = -EPROTO; goto finish; }
    if ((size_t)result > capacity) { result = -EMSGSIZE; goto finish; }
    memcpy(bytes, (const void *)(uintptr_t)properties.buffer, (size_t)result);
    errno = 0;
    rc = api.advance(t->rx, slot);
    if (!rc) { errno = 0; rc = api.sync(t->channel, 1); }
    if (rc) { result = t->fatal = api_error(rc); goto finish; }
    *type = rx_types[index];
finish:
    pthread_mutex_unlock(&t->lock);
    return result;
}

static int monotonic_ns(int64_t *value) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -errno;
    *value = (int64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    return 0;
}

int acipc_recv(uint8_t *type, uint8_t *bytes, size_t capacity, int timeout_ms) {
    if (!type || !bytes || !capacity || timeout_ms < 0) return -EINVAL;
    if (!opened) return -ENOTCONN;
    int64_t now;
    int error = monotonic_ns(&now);
    if (error) return error;
    const int64_t deadline = now + (int64_t)timeout_ms * 1000000;
    struct pollfd fds[3];
    for (unsigned i = 0; i < 3; ++i)
        fds[i] = (struct pollfd){.fd = transports[i].fd, .events = POLLIN};
    for (;;) {
        for (unsigned offset = 0; offset < 3; ++offset) {
            unsigned index = (receive_start + offset) % 3;
            int result = receive_one(index, type, bytes, capacity);
            if (result) {
                receive_start = (index + 1) % 3;
                return result;
            }
        }
        error = monotonic_ns(&now);
        if (error) return error;
        if (now >= deadline) return 0;
        int64_t remaining = deadline - now;
        int wait_ms = (int)((remaining + 999999) / 1000000);
        if (wait_ms > 10) wait_ms = 10;
        int rc = poll(fds, 3, wait_ms);
        if (rc < 0 && errno != EINTR) return -errno;
        if (rc > 0) {
            for (unsigned i = 0; i < 3; ++i)
                if (fds[i].revents & (POLLERR | POLLHUP | POLLNVAL)) return -EIO;
            /* A ready fd with no slot must not cause a busy loop. */
            error = monotonic_ns(&now);
            if (error) return error;
            remaining = deadline - now;
            if (remaining > 0) {
                struct timespec pause = {.tv_sec = 0,
                    .tv_nsec = remaining < 1000000 ? (long)remaining : 1000000};
                if (nanosleep(&pause, NULL) && errno != EINTR) return -errno;
            }
        }
    }
}

#ifdef ACIPC_BRIDGE_TEST
/* These tests replace discovery and every channel API. No hardware is opened. */
#include <assert.h>
#include <stdio.h>
#include <stdatomic.h>
#include <unistd.h>
typedef struct {
    unsigned index;
    int rx;
} MockRing;
typedef struct {
    MockRing tx, rx;
    uint8_t tx_bytes[4096], rx_bytes[4096];
    size_t tx_capacity, rx_length;
    int tx_available, rx_available, defunct, fail_sync, fail_advance;
    int destroyed, set_count, advance_count, sync_count;
    size_t sent_length;
    atomic_int syncing;
} MockChannel;
static MockChannel mocks[3];
static int fail_create = -1, mock_pipe[2], attr_destroyed;
static int attr_token;
static void *mock_attr_create(void) { return &attr_token; }
static void mock_attr_destroy(void *attr) {
    assert(attr == &attr_token);
    ++attr_destroyed;
}
static int mock_attr_set(void *attr, int kind, uint64_t value) {
    assert(attr == &attr_token);
    assert((kind == 6 && value == 0) || (kind == 7 && value == 1));
    return 0;
}
static void *mock_create(const uuid_t id, uint32_t port, int direction,
                         uint32_t ring, void *attr) {
    assert(port == 0 && direction == 0 && ring == UINT32_MAX && attr == &attr_token);
    unsigned index = id[0];
    assert(index < 3);
    if ((int)index == fail_create) { errno = EBUSY; return NULL; }
    return &mocks[index];
}
static void mock_destroy(void *channel) { ++((MockChannel *)channel)->destroyed; }
static uint32_t mock_ring_id(void *channel, int direction) {
    assert(channel);
    assert(direction == 0 || direction == 2);
    return direction == 0 ? 41 : 82;
}
static void *mock_tx_ring(void *channel, uint32_t id) {
    assert(id == 41);
    return &((MockChannel *)channel)->tx;
}
static void *mock_rx_ring(void *channel, uint32_t id) {
    assert(id == 82);
    return &((MockChannel *)channel)->rx;
}
static void *mock_next(void *ring, void *previous, Slot *properties) {
    assert(!previous);
    MockRing *r = ring;
    MockChannel *m = &mocks[r->index];
    if (!(r->rx ? m->rx_available : m->tx_available)) return NULL;
    properties->buffer = (uint64_t)(uintptr_t)(r->rx ? m->rx_bytes : m->tx_bytes);
    properties->len = (uint16_t)(r->rx ? m->rx_length : m->tx_capacity);
    properties->index = 99;
    return ring;
}
static void mock_set_slot(void *ring, void *slot, const Slot *properties) {
    assert(ring == slot);
    MockRing *r = ring;
    assert(!r->rx && properties->index == 99);
    MockChannel *m = &mocks[r->index];
    ++m->set_count;
    m->sent_length = properties->len;
}
static int mock_advance(void *ring, void *slot) {
    assert(ring == slot);
    MockRing *r = ring;
    MockChannel *m = &mocks[r->index];
    if (m->fail_advance) return EIO;
    ++m->advance_count;
    if (r->rx) m->rx_available = 0;
    return 0;
}
static int mock_sync(void *channel, int direction) {
    MockChannel *m = channel;
    assert(direction == 0 || direction == 1);
    assert(atomic_fetch_add(&m->syncing, 1) == 0);
    ++m->sync_count;
    int rc = m->fail_sync == 1 || m->fail_sync == m->sync_count ? EIO : 0;
    assert(atomic_fetch_sub(&m->syncing, 1) == 1);
    return rc;
}
static int mock_defunct(void *channel) { return ((MockChannel *)channel)->defunct; }
static int mock_get_fd(void *channel) { assert(channel); return mock_pipe[0]; }
static void mock_reset(void) {
    acipc_close();
    memset(mocks, 0, sizeof(mocks));
    for (unsigned i = 0; i < 3; ++i) {
        mocks[i].tx = (MockRing){i, 0};
        mocks[i].rx = (MockRing){i, 1};
        mocks[i].tx_capacity = sizeof(mocks[i].tx_bytes);
        mocks[i].tx_available = 1;
        atomic_init(&mocks[i].syncing, 0);
    }
    fail_create = -1;
    test_discovery = 1;
    attr_destroyed = 0;
}
static void mock_packet(unsigned index, const uint8_t *bytes, size_t length) {
    assert(length <= sizeof(mocks[index].rx_bytes));
    memcpy(mocks[index].rx_bytes, bytes, length);
    mocks[index].rx_length = length;
    mocks[index].rx_available = 1;
}
static void *test_sender(void *unused) {
    (void)unused;
    const uint8_t command[] = {1, 16, 0};
    for (unsigned i = 0; i < 2000; ++i)
        assert(acipc_send(1, command, sizeof(command)) == 0);
    return NULL;
}
static void *test_receiver(void *unused) {
    (void)unused;
    uint8_t type, bytes[64];
    for (unsigned i = 0; i < 2000; ++i)
        assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == 0);
    return NULL;
}
int main(void) {
    assert(pipe(mock_pipe) == 0);
    api = (API){mock_attr_create, mock_attr_destroy, mock_attr_set, mock_create,
        mock_destroy, mock_ring_id, mock_tx_ring, mock_rx_ring, mock_next,
        mock_set_slot, mock_advance, mock_sync, mock_defunct, mock_get_fd};
    api_ready = 1;
    uint8_t bytes[1024] = {0}, type = 99;
    const uint8_t command[] = {1, 16, 0};
    const uint8_t acl[] = {1, 0, 2, 0, 7, 8};
    const uint8_t sco[] = {1, 0, 1, 9};
    const uint8_t event_footer[] = {14, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    for (unsigned packet_type = 1; packet_type <= 4; ++packet_type) {
        size_t header = packet_type == 2 ? 4 : packet_type == 4 ? 2 : 3;
        for (size_t payload = 0; payload <= 255; ++payload) {
            memset(bytes, 0, sizeof(bytes));
            bytes[packet_type == 4 ? 1 : 2] = (uint8_t)payload;
            size_t length = header + payload;
            assert(packet_length(packet_type, bytes, length, 0) == (int)length);
            assert(packet_length(packet_type, bytes, length + 8, 1) == (int)length);
            assert(packet_length(packet_type, bytes, length + 8, 0) == -EPROTO);
            assert(packet_length(packet_type, bytes, length - 1, 1) == -EPROTO);
            assert(packet_length(packet_type, bytes, length + 1, 1) == -EPROTO);
            assert(packet_length(packet_type, bytes, length + 9, 1) == -EPROTO);
        }
    }
    assert(packet_length(5, bytes, 3, 0) == -EINVAL);
    assert(packet_length(1, NULL, 3, 0) == -EINVAL);
    bytes[2] = bytes[3] = 255;
    assert(packet_length(2, bytes, 65539, 0) == 65539);
    assert(packet_length(2, bytes, 65547, 1) == 65539);
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == -ENOTCONN);
    assert(acipc_send(1, command, sizeof(command)) == -ENOTCONN);
    mock_reset();
    test_discovery = 0;
    assert(acipc_open() == -ENODEV && !opened);
    assert(attr_destroyed == 0);
    for (int failure = 0; failure < 3; ++failure) {
        mock_reset();
        fail_create = failure;
        assert(acipc_open() == -EBUSY && !opened && attr_destroyed == 1);
        for (int i = 0; i < 3; ++i) assert(mocks[i].destroyed == (i < failure));
    }
    mock_reset();
    mock_packet(2, sco, sizeof(sco));
    assert(acipc_open() == -EBUSY && !opened);
    for (unsigned i = 0; i < 3; ++i) assert(mocks[i].destroyed == 1);
    assert(mocks[2].advance_count == 0);
    mock_reset();
    mocks[1].fail_sync = 1;
    assert(acipc_open() == -EIO && !opened);
    for (unsigned i = 0; i < 3; ++i) assert(mocks[i].destroyed == 1);
    mock_reset();
    assert(acipc_open() == 0 && acipc_open() == -EALREADY);
    assert(acipc_send(4, command, sizeof(command)) == -EINVAL);
    assert(acipc_send(1, command, 2) == -EPROTO);
    mocks[0].tx_available = 0;
    assert(acipc_send(1, command, sizeof(command)) == -EAGAIN);
    mocks[0].tx_available = 1;
    mocks[0].tx_capacity = 2;
    assert(acipc_send(1, command, sizeof(command)) == -EMSGSIZE);
    assert(mocks[0].set_count == 0);
    mocks[0].tx_capacity = 512;
    assert(acipc_send(1, command, sizeof(command)) == 0);
    assert(acipc_send(2, acl, sizeof(acl)) == 0);
    assert(acipc_send(3, sco, sizeof(sco)) == 0);
    assert(mocks[0].sent_length == sizeof(command));
    assert(!memcmp(mocks[0].tx_bytes, command, sizeof(command)));
    assert(mocks[1].sent_length == sizeof(acl));
    assert(!memcmp(mocks[1].tx_bytes, acl, sizeof(acl)));
    assert(mocks[2].sent_length == sizeof(sco));
    assert(!memcmp(mocks[2].tx_bytes, sco, sizeof(sco)));
    mock_packet(0, event_footer, sizeof(event_footer));
    mock_packet(1, acl, sizeof(acl));
    mock_packet(2, sco, sizeof(sco));
    memset(bytes, 0xaa, sizeof(bytes));
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == 2 && type == 4);
    assert(bytes[0] == 14 && bytes[1] == 0 && bytes[2] == 0xaa);
    mock_packet(0, event_footer, sizeof(event_footer));
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == 6 && type == 2);
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == 4 && type == 3);
    assert(acipc_recv(&type, bytes, 1, 0) == -EMSGSIZE);
    assert(mocks[0].rx_available);
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == 2 && type == 4);
    assert(acipc_recv(&type, bytes, sizeof(bytes), -1) == -EINVAL);
    assert(acipc_recv(NULL, bytes, sizeof(bytes), 0) == -EINVAL);
    int64_t before, after;
    assert(monotonic_ns(&before) == 0);
    assert(acipc_recv(&type, bytes, sizeof(bytes), 25) == 0);
    assert(monotonic_ns(&after) == 0);
    assert(after - before >= 25000000 && after - before < 250000000);
    /* A readable fd without an RX slot must still obey the timeout. */
    assert(write(mock_pipe[1], "x", 1) == 1);
    int syncs_before = mocks[0].sync_count;
    assert(monotonic_ns(&before) == 0);
    assert(acipc_recv(&type, bytes, sizeof(bytes), 25) == 0);
    assert(monotonic_ns(&after) == 0);
    assert(after - before >= 25000000 && after - before < 250000000);
    assert(mocks[0].sync_count - syncs_before < 100);
    assert(read(mock_pipe[0], bytes, 1) == 1);
    pthread_t sender, receiver;
    assert(pthread_create(&sender, NULL, test_sender, NULL) == 0);
    assert(pthread_create(&receiver, NULL, test_receiver, NULL) == 0);
    assert(pthread_join(sender, NULL) == 0 && pthread_join(receiver, NULL) == 0);
    mock_packet(0, event_footer, 3);
    int advances = mocks[0].advance_count;
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == -EPROTO);
    assert(mocks[0].advance_count == advances && mocks[0].rx_available);
    assert(acipc_send(1, command, sizeof(command)) == -EPROTO);
    mock_reset();
    assert(acipc_open() == 0);
    mocks[0].fail_advance = 1;
    assert(acipc_send(1, command, sizeof(command)) == -EIO);
    int sets = mocks[0].set_count;
    mocks[0].fail_advance = 0;
    assert(acipc_send(1, command, sizeof(command)) == -EIO);
    assert(mocks[0].set_count == sets);
    mock_reset();
    assert(acipc_open() == 0);
    mocks[0].fail_sync = 3; /* open, pre-send, then post-advance commit */
    assert(acipc_send(1, command, sizeof(command)) == -EIO);
    assert(mocks[0].advance_count == 1 && mocks[0].set_count == 1);
    mocks[0].fail_sync = 0;
    assert(acipc_send(1, command, sizeof(command)) == -EIO);
    assert(mocks[0].advance_count == 1 && mocks[0].set_count == 1);
    mock_reset();
    assert(acipc_open() == 0);
    mock_packet(0, event_footer, sizeof(event_footer));
    mocks[0].fail_sync = 3; /* open, pre-receive, then release */
    type = 99;
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == -EIO && type == 99);
    assert(mocks[0].advance_count == 1 && !mocks[0].rx_available);
    mock_reset();
    assert(acipc_open() == 0);
    mocks[2].defunct = 1;
    assert(acipc_recv(&type, bytes, sizeof(bytes), 0) == -ENXIO);
    acipc_close();
    acipc_close();
    for (unsigned i = 0; i < 3; ++i) assert(mocks[i].destroyed == 1);
    assert(close(mock_pipe[0]) == 0 && close(mock_pipe[1]) == 0);
    puts("PASS: framing, mocked lifecycle, TX/RX, fairness, timeout, concurrency, errors");
    return 0;
}
#endif
