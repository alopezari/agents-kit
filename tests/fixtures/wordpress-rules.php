<?php
// BAD 1: sqli from $_GET
$id = $_GET['id'];
$wpdb->get_results( "SELECT * FROM {$wpdb->posts} WHERE ID = $id" );
// OK 2: prepared
$wpdb->get_results( $wpdb->prepare( "SELECT * FROM {$wpdb->posts} WHERE ID = %d", $_GET['id'] ) );
// OK 3: absint
$wpdb->get_var( 'SELECT 1 FROM x WHERE id = ' . absint( $_POST['id'] ) );
// BAD 4: sqli from REST param
function cb( $request ) { global $wpdb; $s = $request->get_param( 's' ); return $wpdb->query( "DELETE FROM t WHERE s = '$s'" ); }
// OK 5: table name interpolation only
$wpdb->get_results( "SELECT * FROM {$wpdb->posts} LIMIT 10" );
// BAD 6: xss
echo $_GET['q'];
// OK 7: escaped
echo esc_html( $_GET['q'] );
// BAD 8: rest route without permission callback
register_rest_route( 'x/v1', '/a', [ 'methods' => 'GET', 'callback' => 'cb' ] );
// OK 9: with callback
register_rest_route( 'x/v1', '/b', [ 'methods' => 'GET', 'callback' => 'cb', 'permission_callback' => 'is_user_logged_in' ] );
// BAD 10: unbounded
$q = new WP_Query( [ 'post_type' => 'product', 'posts_per_page' => -1 ] );
// OK 11: bounded
$q = new WP_Query( [ 'post_type' => 'product', 'posts_per_page' => 50 ] );
// BAD 12: query in loop
foreach ( $ids as $id ) { $rows = $wpdb->get_results( $wpdb->prepare( 'SELECT * FROM t WHERE id = %d', $id ) ); }
// OK 13: suppressed with reason
$all = get_posts( [ 'numberposts' => -1 ] ); // nosemgrep: wp-unbounded-query -- at most 12 plans exist.
