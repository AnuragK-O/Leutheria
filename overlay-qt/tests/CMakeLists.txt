find_package(Qt6 REQUIRED COMPONENTS Test)

# One executable per suite, each a ctest test. They link the same
# overlay_core the app ships, and need no display: the logic has no pixels.
function(overlay_add_test name)
    qt_add_executable(${name} ${name}.cpp)
    target_link_libraries(${name} PRIVATE overlay_core Qt6::Test)
    add_test(NAME ${name} COMMAND ${name})
    # Generous: the model suite waits out real linger/settle timers.
    set_tests_properties(${name} PROPERTIES TIMEOUT 60)
endfunction()

overlay_add_test(tst_overlaymodel)
overlay_add_test(tst_assetmanifest)
overlay_add_test(tst_agentclient)
