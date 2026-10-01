cmake_minimum_required(VERSION 3.21)

project(LeutheriaOverlay
    VERSION 0.1.0
    DESCRIPTION "Native activation overlay for Leutheria"
    LANGUAGES CXX)

# The NSWindow layer is Objective-C++; everything else is portable C++.
if(APPLE)
    enable_language(OBJCXX)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_OBJCXX_STANDARD 20)
set(CMAKE_OBJCXX_STANDARD_REQUIRED ON)

# compile_commands.json in the build dir: lets editors (VS Code's C/C++
# extension, clangd) see the Qt include paths and flags CMake actually uses.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# 6.5 is the floor: loadFromModule(), Qt.styleHints.colorScheme and the
# QTP0001 resource layout are all used below.
find_package(Qt6 6.5 REQUIRED COMPONENTS Core Gui Qml Quick WebSockets Multimedia)
qt_standard_project_setup(REQUIRES 6.5)
# The QML lives in qml/, not next to CMakeLists.txt: give that directory its
# own qmldir so its files resolve each other as one module.
qt_policy(SET QTP0004 NEW)
# Module output laid out by URI (qml/Leutheria/Core, ...) so qmllint and
# qmlls can find both modules' type information.
set(QT_QML_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/qml)

# --- overlay_core: all of the logic, no pixels -----------------------------
# A static library so the app and the unit tests compile it once and test
# exactly what ships. It is also a QML module (Leutheria.Core): the
# QML_ELEMENT/QML_UNCREATABLE macros in its headers give QML and qmllint real
# type information for OverlayModel and AssetManifest instead of `var`.

qt_add_library(overlay_core STATIC)
qt_add_qml_module(overlay_core
    URI Leutheria.Core
    VERSION 1.0
    SOURCES
        src/AgentClient.cpp src/AgentClient.h
        src/AssetManifest.cpp src/AssetManifest.h
        src/OverlayModel.cpp src/OverlayModel.h
)
target_include_directories(overlay_core PUBLIC src)
target_link_libraries(overlay_core PUBLIC Qt6::Core Qt6::Qml Qt6::WebSockets)
target_compile_options(overlay_core PRIVATE
    $<$<CXX_COMPILER_ID:AppleClang,Clang,GNU>:-Wall -Wextra -Wpedantic>)

# --- the app bundle ---------------------------------------------------------

qt_add_executable(LeutheriaOverlay MACOSX_BUNDLE
    src/main.cpp
    src/FrameCapture.cpp src/FrameCapture.h
    src/MacWindow.h
)
if(APPLE)
    target_sources(LeutheriaOverlay PRIVATE src/MacWindow_mac.mm)
    target_link_libraries(LeutheriaOverlay PRIVATE "-framework AppKit")
else()
    target_sources(LeutheriaOverlay PRIVATE src/MacWindow_stub.cpp)
endif()

qt_add_qml_module(LeutheriaOverlay
    URI Leutheria.Overlay
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/Island.qml
        qml/AssetSlot.qml
        qml/Orb.qml
        qml/ConfirmCard.qml
        qml/OverlayButton.qml
        qml/Icon.qml
        qml/Theme.qml
)

target_link_libraries(LeutheriaOverlay PRIVATE
    overlay_core
    overlay_coreplugin   # registers Leutheria.Core's types (Q_IMPORT_QML_PLUGIN in main.cpp)
    Qt6::Gui Qt6::Quick Qt6::Multimedia)
target_compile_options(LeutheriaOverlay PRIVATE
    $<$<CXX_COMPILER_ID:AppleClang,Clang,GNU>:-Wall -Wextra -Wpedantic>)

set_target_properties(LeutheriaOverlay PROPERTIES
    MACOSX_BUNDLE_GUI_IDENTIFIER dev.leutheria.overlay
    MACOSX_BUNDLE_BUNDLE_NAME LeutheriaOverlay
    MACOSX_BUNDLE_BUNDLE_VERSION ${PROJECT_VERSION}
    MACOSX_BUNDLE_SHORT_VERSION_STRING ${PROJECT_VERSION}
    # The template adds LSUIElement: an overlay has no business in the Dock
    # or the app switcher.
    MACOSX_BUNDLE_INFO_PLIST ${CMAKE_CURRENT_SOURCE_DIR}/cmake/Info.plist.in)

# --- tests ------------------------------------------------------------------

include(CTest)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
