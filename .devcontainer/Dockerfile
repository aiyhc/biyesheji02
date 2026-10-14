# 开发容器镜像：基于乐鑫官方 ESP-IDF 镜像
# 用于在 VS Code Dev Containers 里获得一致的 ESP-IDF 编译环境
ARG DOCKER_TAG=latest
FROM espressif/idf:${DOCKER_TAG}

# 统一字符编码，避免中文日志/文件名乱码
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# udev：容器内识别并访问串口设备（烧录/监视需要）
RUN apt-get update -y && apt-get install udev -y

# 进入容器自动加载 ESP-IDF 环境变量（idf.py 等命令可用）
RUN echo "source /opt/esp/idf/export.sh > /dev/null 2>&1" >> ~/.bashrc

# 使用官方入口脚本，默认打开 bash
ENTRYPOINT [ "/opt/esp/entrypoint.sh" ]
CMD ["/bin/bash", "-c"]
