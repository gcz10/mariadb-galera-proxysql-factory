FROM rockylinux:9

# SSH server + narzędzia systemowe potrzebne dla Ansible
RUN dnf -y update && dnf -y install \
    openssh-server \
    sudo \
    python3 \
    python3-pip \
    chrony \
    firewalld \
    selinux-policy \
    libselinux-utils \
    policycoreutils \
    iproute \
    iputils \
    bind-utils \
    numactl \
    less \
    vim \
    && dnf clean all

# Ansible user z sudo bez hasla
RUN useradd -m -s /bin/bash ansible && \
    echo 'ansible:ansible' | chpasswd && \
    echo 'ansible ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ansible

# SSH keys
RUN mkdir -p /home/ansible/.ssh && chmod 700 /home/ansible/.ssh && \
    chown ansible:ansible /home/ansible/.ssh

# SSH host keys
RUN ssh-keygen -A

# SSH config: allow key auth
RUN sed -i 's/^#PubkeyAuthentication yes/PubkeyAuthentication yes/' /etc/ssh/sshd_config && \
    sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

# SELinux — kontenery Docker domyslnie nie enforce SELinux (zaleta: brak jadra z SELinux)
# F0 discovery zanotuje to jako "SELinux not available in container"

EXPOSE 22

# Start SSH (kontener musi dzialac jako daemon)
CMD ["/usr/sbin/sshd", "-D"]
