def update_node_feature_redis(modeladmin, request, queryset):
    """
    Update the node feature in redis from admin interface
    :return:
    """
    for query in queryset:
        query._push_to_redis()
        # todo add support to write to kafka in the future
