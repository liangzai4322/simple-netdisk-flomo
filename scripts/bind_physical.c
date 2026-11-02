#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
#include <stdio.h>

/* Scope only this child process's Internet sockets to the physical interface.
 * Fail closed: a socket must not fall back through the system TUN. */
static int physical_connect(int fd, const struct sockaddr *address, socklen_t length) {
    if (address && (address->sa_family == AF_INET || address->sa_family == AF_INET6)) {
        const char *name = getenv("NETDISK_PHYSICAL_INTERFACE");
        unsigned int index = name ? if_nametoindex(name) : 0;
        int level = address->sa_family == AF_INET ? IPPROTO_IP : IPPROTO_IPV6;
        int option = address->sa_family == AF_INET ? IP_BOUND_IF : IPV6_BOUND_IF;
        if (!index || setsockopt(fd, level, option, &index, sizeof(index))) {
            errno = ENETUNREACH;
            return -1;
        }
        char host[INET6_ADDRSTRLEN] = {0};
        const void *ip = address->sa_family == AF_INET
            ? (const void *)&((const struct sockaddr_in *)address)->sin_addr
            : (const void *)&((const struct sockaddr_in6 *)address)->sin6_addr;
        inet_ntop(address->sa_family, ip, host, sizeof(host));
        unsigned int bound = 0;
        socklen_t size = sizeof(bound);
        if (getsockopt(fd, level, option, &bound, &size) || bound != index) {
            errno = ENETUNREACH;
            return -1;
        }
        fprintf(stderr, "[physical] interface=%s index=%u destination=%s\n", name, bound, host);
    }
    return connect(fd, address, length);
}
__attribute__((used)) static struct { const void *replacement; const void *original; }
interpose_connect __attribute__((section("__DATA,__interpose"))) = {
    (const void *)physical_connect, (const void *)connect
};
