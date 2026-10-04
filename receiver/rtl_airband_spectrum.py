"""Adds the spectrum tap to rtl_airband (v5.4.2): run in its source tree's top directory, before building.

rtl_airband finds its channels with an FFT of everything the dongle hears. This makes it
send that spectrum out as well (see the comment in the C++ below), which is what the
receiver's waterfall is drawn from. install.sh runs it; running it twice does nothing."""
import sys

p = "src/rtl_airband.cpp"; s = open(p).read()
if "spectrum_tap" in s:
    print("already patched")
    sys.exit(0)
inc = '#include <unistd.h>\n'
assert s.count(inc) == 1
s = s.replace(inc, inc + '#include <arpa/inet.h>\n#include <netinet/in.h>\n#include <sys/socket.h>\n')
anchor = "void* demodulate(void* params) {"
assert s.count(anchor) == 1
tap = r'''#ifndef WITH_BCM_VC
// Sleep Radio: the spectrum the channelizer's FFT has already worked out, sent out for a
// waterfall. With SPECTRUM_UDP_PORT=N in the environment, ten times a second a datagram goes
// to 127.0.0.1:N: fft_size float32s, the mean power of each FFT bin (in the FFT's own order:
// bin 0 is the centre frequency, the upper half is below it). One FFT in SPECTRUM_EVERY is
// looked at, so the cost is a few thousand multiplications a second.
#define SPECTRUM_EVERY 64
#define SPECTRUM_FRAMES 25
static void spectrum_tap(fftwf_complex* out) {
    static int sock = -2;
    static struct sockaddr_in to;
    static std::vector<float> acc;
    static unsigned skipped = 0, frames = 0;
    if (sock == -2) {
        const char* port = getenv("SPECTRUM_UDP_PORT");
        sock = (port && atoi(port) > 0) ? socket(AF_INET, SOCK_DGRAM, 0) : -1;
        if (sock >= 0) {
            memset(&to, 0, sizeof(to));
            to.sin_family = AF_INET;
            to.sin_port = htons((uint16_t)atoi(port));
            to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            acc.assign(fft_size, 0.0f);
        }
    }
    if (sock < 0 || ++skipped < SPECTRUM_EVERY)
        return;
    skipped = 0;
    for (size_t i = 0; i < fft_size; i++)
        acc[i] += out[i][0] * out[i][0] + out[i][1] * out[i][1];
    if (++frames < SPECTRUM_FRAMES)
        return;
    for (size_t i = 0; i < fft_size; i++)
        acc[i] /= (float)frames;
    sendto(sock, acc.data(), fft_size * sizeof(float), MSG_DONTWAIT, (struct sockaddr*)&to, sizeof(to));
    acc.assign(fft_size, 0.0f);
    frames = 0;
}
#endif /* WITH_BCM_VC */

'''
s = s.replace(anchor, tap + anchor)
call = '''        fftwf_execute(demod_params->fft);
#endif /* WITH_BCM_VC */
'''
assert s.count(call) == 1
s = s.replace(call, '''        fftwf_execute(demod_params->fft);
        spectrum_tap(fftout);
#endif /* WITH_BCM_VC */
''')
open(p, "w").write(s)
print("patched")
