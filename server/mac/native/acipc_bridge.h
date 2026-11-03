#ifndef ACIPC_BRIDGE_H
#define ACIPC_BRIDGE_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* All errors are negative errno values. send returns 0 on success.
 * recv returns the canonical packet length, or 0 when its timeout expires.
 * Packet buffers do not contain an H4 type byte or an Apple RX footer.
 * One send thread and one receive thread are supported. Do not call open or
 * close while either thread uses the bridge. Stop and join both before close.
 * A TX commit error can mean the packet was sent: do not retry it blindly.
 * A malformed RX packet stays in its slot and returns -EPROTO.
 */
int acipc_open(void);
int acipc_send(uint8_t type, const uint8_t *bytes, size_t length);
int acipc_recv(uint8_t *type, uint8_t *bytes, size_t capacity, int timeout_ms);
void acipc_close(void);
#ifdef __cplusplus
}
#endif
#endif
