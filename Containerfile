# =============================================================================
# Stage 1 — builder
#   Compiles librtlsdr and rtl_ais from source using Fedora, which has a
#   complete devel package set. libusb-devel is not published in UBI 9 repos.
#   All build tools and devel headers stay in this stage only.
# =============================================================================
FROM fedora:latest AS builder

RUN dnf install --nodocs -y \
        cmake make gcc gcc-c++ libusb1-devel pkg-config git \
    && dnf clean all

# --- librtlsdr ----------------------------------------------------------------
RUN git clone --depth 1 https://github.com/osmocom/rtl-sdr.git /tmp/rtl-sdr

RUN cmake -S /tmp/rtl-sdr -B /tmp/rtl-sdr/build \
        -DCMAKE_INSTALL_PREFIX=/opt/rtlsdr \
        -DINSTALL_UDEV_RULES=OFF \
    && cmake --build /tmp/rtl-sdr/build --parallel "$(nproc)" \
    && cmake --install /tmp/rtl-sdr/build \
    && ldconfig /opt/rtlsdr/lib /opt/rtlsdr/lib64

# --- rtl_ais ------------------------------------------------------------------
# rtl-ais uses a plain Makefile, not CMake.
# cmake installs librtlsdr to lib/ on x86_64 and lib64/ on aarch64; cover both.
RUN git clone --depth 1 https://github.com/dgiardini/rtl-ais.git /tmp/rtl-ais

RUN PKG_CONFIG_PATH=/opt/rtlsdr/lib/pkgconfig:/opt/rtlsdr/lib64/pkgconfig \
    make -C /tmp/rtl-ais \
        CFLAGS="-O2 -I/opt/rtlsdr/include -I./aisdecoder -I./aisdecoder/lib -I./tcp_listener" \
        LDFLAGS="-L/opt/rtlsdr/lib -L/opt/rtlsdr/lib64 -Wl,-rpath,/opt/rtlsdr/lib:/opt/rtlsdr/lib64 -lrtlsdr -lm -lpthread" \
        -j"$(nproc)" \
    && install -m 0755 /tmp/rtl-ais/rtl_ais /opt/rtlsdr/bin/rtl_ais

# =============================================================================
# Stage 2 — final
#   Based on the hi/python hardened image. This image has NO shell (/bin/sh,
#   bash, etc.) — all RUN instructions must use exec (JSON array) form.
#   Binaries and libraries are copied from the builder stage; no package
#   manager calls are made here.
# =============================================================================
FROM registry.access.redhat.com/hi/python:latest

# The hi/python image runs as uid 65532 (non-root). Only /tmp is writable by
# default. Switch to root to create /app, install files, then drop back.
USER root

# Create /app owned by the runtime user (65532) so it is writable at runtime.
RUN ["python3", "-c", "import os; os.makedirs('/app/lib', exist_ok=True); os.makedirs('/app/app', exist_ok=True); os.makedirs('/app/static', exist_ok=True); [os.chown(p, 65532, 0) for p in ['/app', '/app/lib', '/app/app', '/app/static']]"]

# Copy the compiled rtlsdr tree (binaries + shared libraries).
COPY --from=builder /opt/rtlsdr /opt/rtlsdr

# Copy libusb1 runtime shared libraries from the builder stage.
# hi/python has no package manager, so we copy directly from the builder.
COPY --from=builder /usr/lib64/libusb-1.0.so* /usr/lib64/

ENV PATH="/opt/rtlsdr/bin:${PATH}"
# Include both lib and lib64 — cmake uses lib64 on aarch64, lib on x86_64.
ENV LD_LIBRARY_PATH="/opt/rtlsdr/lib:/opt/rtlsdr/lib64:${LD_LIBRARY_PATH}"
ENV PYTHONPATH="/app/lib"

# --- Python dependencies ------------------------------------------------------
WORKDIR /app

COPY requirements.txt .

# Install to /app/lib — a stable, known location on PYTHONPATH.
RUN ["pip", "install", "--no-cache-dir", "--target", "/app/lib", "-r", "requirements.txt"]

# --- Application source -------------------------------------------------------
COPY app/ /app/app/
COPY static/ /app/static/

# Drop back to the non-root runtime user.
USER 65532

# --- Environment variable defaults --------------------------------------------
ENV PORT=8080
ENV RTL_GAIN=0
ENV RTL_PPM=0
ENV RTL_UDP_PORT=10110
ENV FORWARD_TARGETS=""

EXPOSE 8080

# Invoke via python3 -m — console scripts in /tmp/.local/bin are not on PATH.
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
