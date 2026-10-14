# ESP-IDF 工程最顶层 CMakeLists.txt
# 以下固定写法必须保持这个顺序，cmake 才能正确加载 IDF 的构建脚本
cmake_minimum_required(VERSION 3.16)

# 引入 ESP-IDF 提供的 project.cmake（依赖环境变量 IDF_PATH）
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# 声明工程名，会作为生成的固件名：02biyesheji.bin
project(02biyesheji)
