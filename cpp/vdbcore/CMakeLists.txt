# The OpenVDB kernel's C++ core (nanobind). Built by scripts/build_vdbcore.py,
# which puts the module into src/process_studio/kernels/openvdb/.
cmake_minimum_required(VERSION 3.18)
project(vdbcore LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(Python 3.11 COMPONENTS Interpreter Development.Module REQUIRED)
execute_process(COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
                OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE nanobind_ROOT)
find_package(nanobind CONFIG REQUIRED)

# OpenVDB from a conda environment, Homebrew or the system: the headers and
# the library are all this needs (its own CMake config is not shipped by
# every distribution).
set(_hints $ENV{CONDA_PREFIX} $ENV{OPENVDB_ROOT} /opt/homebrew /usr/local /usr)
find_path(OPENVDB_INCLUDE openvdb/openvdb.h HINTS ${_hints} PATH_SUFFIXES include REQUIRED)
find_library(OPENVDB_LIB openvdb HINTS ${_hints} PATH_SUFFIXES lib REQUIRED)
find_package(TBB REQUIRED)

nanobind_add_module(_vdbcore NB_STATIC vdbcore.cpp)
target_include_directories(_vdbcore PRIVATE ${OPENVDB_INCLUDE})
target_link_libraries(_vdbcore PRIVATE ${OPENVDB_LIB} TBB::tbb)
if(MSVC)
  target_compile_options(_vdbcore PRIVATE /O2 /bigobj)
else()
  target_compile_options(_vdbcore PRIVATE -O3)
endif()
install(TARGETS _vdbcore LIBRARY DESTINATION .)
