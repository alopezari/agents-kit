<?php
update_option( 'x_enabled', false );            // hit
update_option( 'x_enabled', (bool) $v );        // hit
update_option( 'x_enabled', ! $off );           // hit
update_option( 'x_enabled', $v ? 1 : 0 );       // ok
update_option( 'run_state', $state, false );    // ok: autoload arg
update_option( 'x_enabled', 0 );                // ok
